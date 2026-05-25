"""Streaming watchdog with three independent liveness gates.

Stream emission from upstream providers is bursty by design: tokens arrive
in short bursts separated by multi-second inter-burst gaps. A rolling-window
TPS check trips on the gaps even when aggregate throughput is healthy and
the response eventually completes correctly. We therefore use three timer
gates instead:

* ``ttft`` — no first chunk within the time-to-first-token budget;
* ``stall`` — no chunk within the inter-chunk inactivity budget;
* ``total`` — overall wall-clock budget for the whole stream.

Liveness counts every chunk that carries committed model output: visible
text, reasoning_content, and tool-call deltas. A reasoning-heavy model that
streams ``delta.reasoning_content`` for tens of seconds before any visible
answer keeps the stream alive — ``total_wall_seconds`` is the safety net
against degenerate models that reason forever without producing text.

The inactivity gate has two thresholds: a strict ``inactivity_seconds`` for
text or reasoning chunks, and a relaxed ``tool_call_inactivity_seconds`` that
applies after a tool-call-only chunk. Provider built-in tools (e.g. OpenAI's
``web_search``) surface lifecycle progress as Chat Completions
``delta.tool_calls`` keepalives; the strict gap is too tight for the long
upstream phases between them. Text or reasoning content snaps the gate back
to strict mode so a model that genuinely stalls after speaking is still
killed quickly.

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
    """Thresholds for the watchdog gates and the ticker period."""

    ttft_seconds: float = 60.0
    inactivity_seconds: float = 30.0
    tool_call_inactivity_seconds: float = 120.0
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
    _last_chunk_was_tool_call: bool = field(init=False, default=False)

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

    def on_content(self, *, text_tokens: int = 0, tool_call_tokens: int = 0, reasoning_tokens: int = 0) -> None:
        """Record committed model output from the latest streaming chunk.

        ``text_tokens``, ``tool_call_tokens``, and ``reasoning_tokens``
        partition the chunk's signal. Any of the three keeps the stream
        alive. The relaxed inactivity gate flips on only for chunks that
        carry tool-call deltas alone — text or reasoning resets it to the
        strict gate.
        """
        total = text_tokens + tool_call_tokens + reasoning_tokens
        if not self.enabled or total <= 0 or self._t_start is None:
            return
        now = time.monotonic()
        if self._t_first_content is None:
            self._t_first_content = now
        self._t_last_chunk = now
        self._content_tokens += total
        self._last_chunk_was_tool_call = text_tokens == 0 and reasoning_tokens == 0 and tool_call_tokens > 0

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
        inactivity_limit = (
            self.config.tool_call_inactivity_seconds
            if self._last_chunk_was_tool_call
            else self.config.inactivity_seconds
        )
        if now - self._t_last_chunk > inactivity_limit:
            raise StreamWatchdogError("stall", self.deployment_id, self.final_tps())

    def final_tps(self) -> float:
        """Observed visible-content tokens-per-second for telemetry."""
        if self._t_first_content is None:
            return 0.0
        elapsed = time.monotonic() - self._t_first_content
        return self._content_tokens / elapsed if elapsed > 0 else 0.0
