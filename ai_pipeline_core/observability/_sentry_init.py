"""Thread-safe Sentry SDK initialization."""

from threading import Lock

import sentry_sdk

__all__ = ["_reset_for_testing", "ensure_sentry_initialized"]

_INIT_LOCK = Lock()
_initialized = False


def ensure_sentry_initialized(dsn: str | None) -> None:
    """Initialize the global Sentry SDK exactly once per process."""
    global _initialized  # noqa: PLW0603

    if not dsn:
        return

    with _INIT_LOCK:
        if _initialized:
            return
        sentry_sdk.init(
            dsn=dsn,
            default_integrations=False,
            integrations=[],
            traces_sample_rate=0.0,
            auto_enabling_integrations=False,
        )
        _initialized = True


def _reset_for_testing() -> None:
    """Reset module state so tests can reinitialize Sentry independently."""
    global _initialized  # noqa: PLW0603
    with _INIT_LOCK:
        _initialized = False
