"""Concurrency and rate limiting for pipeline deployments.

Wraps Prefect's global concurrency limits so app code never imports from Prefect.
Limits are declared on PipelineDeployment subclasses and enforced via pipeline_concurrency().

Supports three limit kinds:
- CONCURRENT: Lease-based slots held during operation, released on exit.
- PER_MINUTE: Token bucket with limit/60 decay per second (allows bursting).
- PER_HOUR: Token bucket with limit/3600 decay per second (allows bursting).

When Prefect is unavailable, all limits proceed unthrottled with a warning.
"""

import logging
import re
import time
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from prefect.client.orchestration import get_client
from prefect.concurrency.asyncio import (
    AcquireConcurrencySlotTimeoutError,
    ConcurrencySlotAcquisitionError,
    concurrency,
    rate_limit,
)

logger = logging.getLogger(__name__)

_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600
_VALID_LIMIT_NAME = re.compile(r"^[a-zA-Z0-9_-]+$")
_CACHE_TTL_WARNING_THRESHOLD = 120  # Warn if slot wait exceeds 2 minutes (cache TTL default is 5 min)


class LimitKind(StrEnum):
    """Kind of concurrency/rate limit.

    CONCURRENT: Slots held for duration of operation (lease-based).
        limit=500 means at most 500 simultaneous operations across all runs.

    PER_MINUTE: Token bucket with limit/60 decay per second.
        Allows bursting up to `limit` immediately, then refills gradually.
        NOT a sliding window.

    PER_HOUR: Token bucket with limit/3600 decay per second. Same burst semantics.
    """

    CONCURRENT = "concurrent"
    PER_MINUTE = "per_minute"
    PER_HOUR = "per_hour"


@dataclass(frozen=True, slots=True)
class PipelineLimit:
    """Concurrency/rate limit configuration.

    Must use names matching ``[a-zA-Z0-9_-]+`` in PipelineDeployment.concurrency_limits
    (validated at class definition time).
    """

    limit: int
    kind: LimitKind = LimitKind.CONCURRENT
    timeout: int = 600

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError(f"limit must be >= 1, got {self.limit}")
        if self.timeout <= 0:
            raise ValueError(f"timeout must be > 0, got {self.timeout}")


# ---------------------------------------------------------------------------
# Internal state — ContextVar with mutable shared status
# ---------------------------------------------------------------------------


class _SharedStatus:
    """Mutable shared state across all tasks within a single pipeline run.

    Stored inside the ContextVar's frozen dataclass so all tasks forked via
    asyncio.gather share the same instance. ContextVar propagation copies the
    dataclass reference, but all copies point to the same _SharedStatus object.
    """

    __slots__ = ("prefect_available",)

    def __init__(self) -> None:
        self.prefect_available: bool = True


@dataclass(frozen=True, slots=True)
class _LimitsState:
    """Immutable container holding limit config and mutable shared status."""

    limits: Mapping[str, PipelineLimit]
    status: _SharedStatus


_EMPTY_STATE = _LimitsState(limits=MappingProxyType({}), status=_SharedStatus())
_limits_state: ContextVar[_LimitsState] = ContextVar("_pipeline_limits_state", default=_EMPTY_STATE)


def _set_limits_state(state: _LimitsState) -> Token[_LimitsState]:
    """Set limits state for the current scope."""
    return _limits_state.set(state)


# ---------------------------------------------------------------------------
# Public context manager
# ---------------------------------------------------------------------------


@asynccontextmanager
async def pipeline_concurrency(
    name: str,
    *,
    timeout: int | None = None,
) -> AsyncGenerator[None]:
    """Acquire a concurrency/rate-limit slot for an operation.

    For CONCURRENT limits: slot held during block, released on exit.
    For PER_MINUTE/PER_HOUR: slot acquired (decays automatically), exit is no-op.

    Proceeds unthrottled when Prefect is unavailable.
    Timeout always raises AcquireConcurrencySlotTimeoutError.
    Logs a warning if slot acquisition takes longer than 120 seconds.
    """
    state = _limits_state.get()
    cfg = state.limits.get(name)
    if cfg is None:
        available = ", ".join(sorted(state.limits)) or "(none)"
        raise KeyError(
            f"pipeline_concurrency({name!r}) not registered. Available limits: {available}. "
            "Declare it on PipelineDeployment.concurrency_limits."
        )

    # Prefect unavailable — proceed unthrottled
    if not state.status.prefect_available:
        yield
        return

    effective_timeout = timeout if timeout is not None else cfg.timeout
    t0 = time.monotonic()

    def _warn_if_slow() -> None:
        wait_seconds = time.monotonic() - t0
        if wait_seconds > _CACHE_TTL_WARNING_THRESHOLD:
            logger.warning(
                "Slot wait for %r took %.1fs — exceeds %ds threshold. "
                "LLM cache TTL (default 300s) may expire before execution. "
                "Consider increasing concurrency limit or reducing parallelism.",
                name,
                wait_seconds,
                _CACHE_TTL_WARNING_THRESHOLD,
            )

    # Prefect available — use global concurrency/rate limiting
    try:
        match cfg.kind:
            case LimitKind.CONCURRENT:
                async with concurrency(name, occupy=1, timeout_seconds=effective_timeout, strict=False):
                    _warn_if_slow()
                    yield
            case LimitKind.PER_MINUTE | LimitKind.PER_HOUR:
                await rate_limit(name, occupy=1, timeout_seconds=effective_timeout, strict=False)
                _warn_if_slow()
                yield
    except AcquireConcurrencySlotTimeoutError:
        raise
    except ConcurrencySlotAcquisitionError as e:
        logger.warning("Prefect concurrency unavailable for %r, proceeding unthrottled: %s", name, e)
        state.status.prefect_available = False
        yield


# ---------------------------------------------------------------------------
# Upsert — called at start of PipelineDeployment.run()
# ---------------------------------------------------------------------------


def _slot_decay_per_second(cfg: PipelineLimit) -> float:
    """Compute Prefect slot_decay_per_second. Returns 0.0 for CONCURRENT (explicit, not None)."""
    match cfg.kind:
        case LimitKind.CONCURRENT:
            return 0.0
        case LimitKind.PER_MINUTE:
            return cfg.limit / _SECONDS_PER_MINUTE
        case LimitKind.PER_HOUR:
            return cfg.limit / _SECONDS_PER_HOUR


async def _ensure_concurrency_limits(limits: Mapping[str, PipelineLimit]) -> None:
    """Idempotently create/update all concurrency limits in Prefect server.

    Called at start of PipelineDeployment.run(). If Prefect is unavailable,
    logs warning and sets prefect_available=False (falls back to local semaphores).
    """
    if not limits:
        return
    state = _limits_state.get()
    try:
        async with get_client() as client:
            for name, cfg in limits.items():
                await client.upsert_global_concurrency_limit_by_name(
                    name=name,
                    limit=cfg.limit,
                    slot_decay_per_second=_slot_decay_per_second(cfg),
                )
                logger.debug("Concurrency limit '%s': limit=%d, kind=%s", name, cfg.limit, cfg.kind)
    except Exception as e:
        logger.warning("Prefect unavailable; falling back to local semaphores: %s", e)
        state.status.prefect_available = False


# ---------------------------------------------------------------------------
# Validation — called from PipelineDeployment.__init_subclass__
# ---------------------------------------------------------------------------


def _validate_concurrency_limits(
    deployment_name: str,
    raw: Mapping[str, PipelineLimit],
) -> Mapping[str, PipelineLimit]:
    """Validate concurrency_limits at class definition time. Returns immutable copy."""
    if not raw:
        return MappingProxyType({})
    for name, config in raw.items():
        if not isinstance(name, str):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(f"{deployment_name}.concurrency_limits key must be str, got {type(name).__name__}")
        if not _VALID_LIMIT_NAME.match(name):
            raise TypeError(f"{deployment_name}.concurrency_limits: invalid name '{name}'. Must match [a-zA-Z0-9_-]+")
        if not isinstance(config, PipelineLimit):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(
                f"{deployment_name}.concurrency_limits['{name}'] must be PipelineLimit, got {type(config).__name__}"
            )
        if not isinstance(config.kind, LimitKind):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(
                f"{deployment_name}.concurrency_limits['{name}'].kind must be LimitKind, "
                f"got {type(config.kind).__name__}"
            )
    return MappingProxyType(dict(raw))


__all__ = [
    "LimitKind",
    "PipelineLimit",
    "_ensure_concurrency_limits",
    "_set_limits_state",
    "_validate_concurrency_limits",
    "pipeline_concurrency",
]
