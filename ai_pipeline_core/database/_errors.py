"""Classification of database write failures as transient vs deterministic.

Used by the span retry buffer to decide whether a failed batch should be kept
together (transient connectivity blip — the whole batch will likely succeed once
the DB recovers) or force-split to isolate a poison row (deterministic data
rejection). Keeping the ClickHouse exception knowledge here keeps the pipeline
layer backend-agnostic.
"""

from clickhouse_connect.driver.exceptions import DatabaseError as ClickHouseDatabaseError

__all__ = ["is_transient_db_error"]

# ConnectionError covers the circuit-breaker's fast-fail signal and raw socket
# resets; OSError/TimeoutError cover lower-level network failures.
#
# ClickHouseDatabaseError is included as the broad parent because the backend's
# own _insert() catches and re-raises (ClickHouseDatabaseError, ConnectionError,
# OSError) indiscriminately for its circuit-breaker logic. The clickhouse_connect
# driver wraps transient network/session failures in various DatabaseError
# subclasses (OperationalError, InternalError, plain DatabaseError). Treating the
# full family as transient prevents premature batch-splitting during genuine flaps.
#
# Genuine deterministic poison (e.g. DataError for bad data) also inherits from
# DatabaseError and is therefore classified transient here — but _resolve_batch
# increments the per-row attempt counter on EVERY failure regardless of
# classification, and force-splits any batch whose members exceed the attempt cap.
# This guarantees poison rows are eventually isolated and quarantined even when
# misclassified as transient; it just takes MAX_ATTEMPTS batch retries to trigger
# the split instead of splitting immediately.
_TRANSIENT_DB_ERRORS: tuple[type[BaseException], ...] = (
    ConnectionError,
    OSError,
    TimeoutError,
    ClickHouseDatabaseError,
)


def is_transient_db_error(exc: BaseException) -> bool:
    """Return True when a failed database write is a transient, retryable failure."""
    return isinstance(exc, _TRANSIENT_DB_ERRORS)
