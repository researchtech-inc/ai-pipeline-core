"""Streaming watchdog with three independent liveness gates.

Stream emission from upstream providers is bursty by design: tokens arrive
in short bursts separated by multi-second inter-burst gaps. A rolling-window
TPS check trips on the gaps even when aggregate throughput is healthy and
the response eventually completes correctly. We therefore use three timer
gates instead:

* ``ttft`` — no first content chunk within the time-to-first-token budget;
* ``stall`` — no chunk within the inter-chunk inactivity budget;
* ``total`` — overall wall-clock budget for the whole stream.

Each gate raises ``StreamWatchdogError`` with a distinct ``reason``. The
``observed_tps`` value carried on the exception is telemetry only and is
never used as a kill criterion.
"""

import os
import time
from dataclasses import dataclass, field

from .exceptions import StreamWatchdogError

__all__ = ["StreamWatchdog", "WatchdogConfig"]


@dataclass(frozen=True, slots=True)
class WatchdogConfig:
    """Thresholds for the three watchdog gates and the ticker period."""

    ttft_seconds: float = 60.0
    inactivity_seconds: float = 30.0
    total_wall_seconds: float = 600.0
    tick_seconds: float = 2.0


@dataclass(slots=True)
class StreamWatchdog:
    """Per-stream watchdog state."""

    deployment_id: str | None = None
    config: WatchdogConfig = field(default_factory=WatchdogConfig)
    enabled: bool = True
    _t_start: float | None = field(init=False, default=None)
    _t_first_content: float | None = field(init=False, default=None)
    _t_last_chunk: float = field(init=False, default=0.0)
    _content_tokens: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        if os.environ.get("AIPL_DISABLE_WATCHDOG", "").lower() in {"1", "true"}:
            self.enabled = False

    def start(self) -> None:
        """Anchor the wall clock once the upstream HTTP response has opened.

        Called by ``StreamSession.__aenter__`` so proxy warmup gates (which
        block before headers return) do not cannibalize the stream budget.
        """
        if self._t_start is None:
            now = time.monotonic()
            self._t_start = now
            self._t_last_chunk = now

    def on_content(self, tokens_added: int) -> None:
        """Record visible content from the latest streaming chunk."""
        if not self.enabled or tokens_added <= 0 or self._t_start is None:
            return
        now = time.monotonic()
        if self._t_first_content is None:
            self._t_first_content = now
        self._t_last_chunk = now
        self._content_tokens += tokens_added

    def check(self) -> None:
        """Raise ``StreamWatchdogError`` when any gate trips."""
        if not self.enabled or self._t_start is None:
            return
        now = time.monotonic()
        if now - self._t_start > self.config.total_wall_seconds:
            raise StreamWatchdogError("total", self.deployment_id, self.final_tps())
        if self._t_first_content is None:
            if now - self._t_start > self.config.ttft_seconds:
                raise StreamWatchdogError("ttft", self.deployment_id, 0.0)
            return
        if now - self._t_last_chunk > self.config.inactivity_seconds:
            raise StreamWatchdogError("stall", self.deployment_id, self.final_tps())

    def final_tps(self) -> float:
        """Observed visible-content tokens-per-second for telemetry."""
        if self._t_first_content is None:
            return 0.0
        elapsed = time.monotonic() - self._t_first_content
        return self._content_tokens / elapsed if elapsed > 0 else 0.0
