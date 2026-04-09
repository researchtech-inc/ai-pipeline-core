"""Protocol types for pipeline internals."""

from typing import Protocol

from ai_pipeline_core.logger._types import LogRecord

__all__ = ["LogSink"]


# Protocol
class LogSink(Protocol):
    """Receives a drained batch of execution-scoped log records."""

    async def on_logs_batch(self, logs: list[LogRecord]) -> None:
        """Persist or forward one drained log batch."""
