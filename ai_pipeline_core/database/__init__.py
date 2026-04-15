"""Unified database module for the span-based schema."""

from ai_pipeline_core.database._factory import Database
from ai_pipeline_core.database._protocol import DatabaseReader
from ai_pipeline_core.database._types import (
    CostTotals,
    DeploymentSummaryRecord,
    DocumentEventPage,
    DocumentEventRecord,
    DocumentProducerRecord,
    DocumentRecord,
    HydratedDocument,
    LogRecord,
    SpanKind,
    SpanRecord,
    SpanStatus,
    aggregate_cost_totals,
)

__all__ = [
    "CostTotals",
    "Database",
    "DatabaseReader",
    "DeploymentSummaryRecord",
    "DocumentEventPage",
    "DocumentEventRecord",
    "DocumentProducerRecord",
    "DocumentRecord",
    "HydratedDocument",
    "LogRecord",
    "SpanKind",
    "SpanRecord",
    "SpanStatus",
    "aggregate_cost_totals",
]
