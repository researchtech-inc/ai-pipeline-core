"""Root logger handler that captures execution-scoped logs for database storage."""

import json
import logging
import traceback
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from ai_pipeline_core.logger._buffer import ExecutionLogBuffer
from ai_pipeline_core.logger._types import LogRecord

__all__ = [
    "SKIP_EXECUTION_LOG_ATTR",
    "ExecutionLogHandler",
    "LogContext",
    "get_log_context",
    "reset_log_context",
    "set_log_context",
]

_APPLICATION_LOG_LEVEL = logging.INFO
_DEPENDENCY_LOG_LEVEL = logging.WARNING
_FRAMEWORK_LOG_LEVEL = logging.DEBUG
SKIP_EXECUTION_LOG_ATTR = "_skip_execution_log"
_DEPENDENCY_LOGGER_PREFIXES = (
    "clickhouse_connect",
    "httpcore",
    "httpx",
    "litellm",
    "prefect",
)
_FRAMEWORK_LOGGER_PREFIX = "ai_pipeline_core"


@dataclass(frozen=True, slots=True)
class LogContext:
    """Minimal execution state needed by the log handler."""

    log_buffer: ExecutionLogBuffer
    span_id: UUID
    deployment_id: UUID


_log_context: ContextVar[LogContext | None] = ContextVar("_log_context", default=None)


def get_log_context() -> LogContext | None:
    """Return the active log context for the current coroutine."""
    return _log_context.get()


def set_log_context(ctx: LogContext | None) -> Token[LogContext | None]:
    """Bind the log context for the current scope."""
    return _log_context.set(ctx)


def reset_log_context(token: Token[LogContext | None]) -> None:
    """Restore the previous log context binding."""
    _log_context.reset(token)


def _matches_prefix(logger_name: str, prefix: str) -> bool:
    """Return whether a logger name matches a namespace prefix exactly or by descendant."""
    return logger_name == prefix or logger_name.startswith(f"{prefix}.")


def _classify_record(record: Any) -> tuple[str, int]:
    """Classify a log record and return the category plus minimum persisted level."""
    if getattr(record, "lifecycle", False):
        return "lifecycle", logging.NOTSET

    if _matches_prefix(record.name, _FRAMEWORK_LOGGER_PREFIX):
        return "framework", _FRAMEWORK_LOG_LEVEL

    if any(_matches_prefix(record.name, prefix) for prefix in _DEPENDENCY_LOGGER_PREFIXES):
        return "dependency", _DEPENDENCY_LOG_LEVEL

    return "application", _APPLICATION_LOG_LEVEL


def _coerce_fields_json(record: Any) -> str:
    """Normalize structured log fields into a JSON string for persistence."""
    raw_fields = getattr(record, "fields_json", "{}")
    if isinstance(raw_fields, str):
        return raw_fields
    return json.dumps(raw_fields, default=str, sort_keys=True)


def _format_exception_text(record: Any) -> str:
    """Render ``exc_info`` into text, ignoring empty exception tuples."""
    if record.exc_info is None or record.exc_info[0] is None:
        return ""
    return "".join(traceback.format_exception(*record.exc_info))


class ExecutionLogHandler(logging.Handler):
    """Route execution-scoped logs from the root logger into the active log buffer."""

    def emit(self, record: Any) -> None:
        """Append an execution-scoped log record to the active buffer when configured."""
        if getattr(record, SKIP_EXECUTION_LOG_ATTR, False):
            return

        ctx = _log_context.get()
        if ctx is None:
            return

        category, minimum_level = _classify_record(record)
        if record.levelno < minimum_level:
            return

        timestamp = datetime.fromtimestamp(record.created, tz=UTC)

        try:
            ctx.log_buffer.append(
                LogRecord(
                    deployment_id=ctx.deployment_id,
                    span_id=ctx.span_id,
                    timestamp=timestamp,
                    sequence_no=0,
                    level=record.levelname,
                    category=category,
                    event_type=str(getattr(record, "event_type", "")),
                    logger_name=record.name,
                    message=record.getMessage(),
                    fields_json=_coerce_fields_json(record),
                    exception_text=_format_exception_text(record),
                )
            )
        except AttributeError, OSError, OverflowError, TypeError, ValueError:
            self.handleError(record)
