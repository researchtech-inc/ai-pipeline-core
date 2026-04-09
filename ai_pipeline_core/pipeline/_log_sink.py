"""Execution log sink implementations."""

from ai_pipeline_core.database._protocol import DatabaseWriter
from ai_pipeline_core.logger._types import LogRecord

__all__ = [
    "DatabaseLogSink",
]


class DatabaseLogSink:
    """Primary durable log sink backed by ``DatabaseWriter.save_logs_batch``."""

    def __init__(self, database: DatabaseWriter) -> None:
        self._database = database

    async def on_logs_batch(self, logs: list[LogRecord]) -> None:
        """Persist the drained batch."""
        await self._database.save_logs_batch(logs)
