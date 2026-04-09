"""Thread-safe Sentry SDK initialization."""

from threading import Lock

import sentry_sdk

__all__ = ["ensure_sentry_initialized"]

_INIT_LOCK = Lock()
_initialized = False


def ensure_sentry_initialized(dsn: str) -> None:
    """Initialize the global Sentry SDK exactly once per process."""
    global _initialized  # noqa: PLW0603

    with _INIT_LOCK:
        if _initialized:
            return
        sentry_sdk.init(dsn=dsn)
        _initialized = True
