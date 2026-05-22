"""Shared defaults for the LLM core."""

DEFAULT_CACHE_TTL_MINUTES = 5
MIN_CACHE_TTL_MINUTES = 1
MAX_CACHE_TTL_MINUTES = 1440  # 24 hours


def cache_ttl_to_wire(minutes: int) -> str | None:
    """Convert integer minutes to the seconds-string TTL format expected upstream.

    Returns ``None`` when caching is disabled (``minutes == 0``); otherwise
    returns ``"{minutes * 60}s"``. Raises ``TypeError`` for non-int inputs and
    ``ValueError`` for values outside ``0`` or ``1..1440``.
    """
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        raise TypeError(f"cache_ttl must be int minutes; got {type(minutes).__name__}.")
    if minutes == 0:
        return None
    if minutes < MIN_CACHE_TTL_MINUTES or minutes > MAX_CACHE_TTL_MINUTES:
        raise ValueError(
            f"cache_ttl must be 0 (off) or between {MIN_CACHE_TTL_MINUTES} "
            f"and {MAX_CACHE_TTL_MINUTES} minutes; got {minutes!r}."
        )
    return f"{minutes * 60}s"
