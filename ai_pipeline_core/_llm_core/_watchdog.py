"""Streaming output-rate watchdog."""

import os
import time
from dataclasses import dataclass, field

from .exceptions import StreamWatchdogError

__all__ = ["DEFAULT_MIN_TPS_FALLBACK", "StreamWatchdog", "WatchdogConfig"]

DEFAULT_MIN_TPS_FALLBACK = 10.0


@dataclass(frozen=True, slots=True)
class WatchdogConfig:
    """Thresholds for one streaming watchdog."""

    grace_seconds: float = 5.0
    grace_tokens: int = 32
    bank_seconds: float = 12.0
    kill_seconds: float = 12.0
    tick_seconds: float = 2.0
    total_wall_seconds: float = 600.0


@dataclass(slots=True)
class StreamWatchdog:
    """Leaky-deficit watchdog for one stream."""

    min_output_tps: float = DEFAULT_MIN_TPS_FALLBACK
    deployment_id: str | None = None
    config: WatchdogConfig = field(default_factory=WatchdogConfig)
    enabled: bool = True
    _t_start: float = field(init=False, default=0.0)
    _t_first_content: float | None = field(init=False, default=None)
    _last_advance: float = field(init=False, default=0.0)
    _content_tokens: int = field(init=False, default=0)
    _credit_tokens: float = field(init=False, default=0.0)
    _observed_tps: float = field(init=False, default=0.0)

    def __post_init__(self) -> None:
        self._t_start = time.monotonic()
        self._last_advance = self._t_start
        if self.min_output_tps <= 0:
            self.min_output_tps = DEFAULT_MIN_TPS_FALLBACK
        if os.environ.get("AIPL_DISABLE_WATCHDOG", "").lower() in {"1", "true"}:
            self.enabled = False

    def on_content(self, tokens_added: int) -> None:
        """Record visible content tokens from the latest stream chunk."""
        if not self.enabled or tokens_added <= 0:
            return
        now = time.monotonic()
        if self._t_first_content is None:
            self._t_first_content = now
            self._last_advance = now
        self._advance_to(now)
        self._content_tokens += tokens_added
        self._credit_tokens += float(tokens_added)
        self._credit_tokens = min(self._credit_tokens, self.config.bank_seconds * self.min_output_tps)

    def check(self) -> None:
        """Raise when the stream violates wall-clock or TPS policy."""
        if not self.enabled:
            return
        now = time.monotonic()
        if now - self._t_start > self.config.total_wall_seconds:
            raise StreamWatchdogError("total", self.deployment_id, self._observed_tps)
        if self._t_first_content is None:
            return
        self._advance_to(now)
        if now - self._t_first_content < self.config.grace_seconds:
            return
        if self._content_tokens < self.config.grace_tokens:
            return
        if self._credit_tokens <= -self.config.kill_seconds * self.min_output_tps:
            raise StreamWatchdogError("tps", self.deployment_id, self._observed_tps)

    def final_tps(self) -> float:
        """Observed visible-content TPS for telemetry."""
        if self._t_first_content is None:
            return 0.0
        elapsed = time.monotonic() - self._t_first_content
        if elapsed <= 0:
            return 0.0
        return self._content_tokens / elapsed

    def _advance_to(self, now: float) -> None:
        elapsed = now - self._last_advance
        if elapsed <= 0:
            return
        self._last_advance = now
        self._credit_tokens -= self.min_output_tps * elapsed
        if self._t_first_content is not None:
            content_elapsed = now - self._t_first_content
            if content_elapsed > 0:
                self._observed_tps = self._content_tokens / content_elapsed
