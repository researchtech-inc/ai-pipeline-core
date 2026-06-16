"""Unified database module for the span-based schema."""

from ai_pipeline_core.database._documents import (
    get_latest_completed_deployment_by_run_id,
    get_latest_result_documents_by_run_ids,
    load_documents_from_database,
    load_latest_documents_by_run_ids,
)
from ai_pipeline_core.database._factory import Database, create_database, create_database_from_settings
from ai_pipeline_core.database._protocol import DatabaseReader, DatabaseWriter
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
    "DatabaseWriter",
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
    "create_database",
    "create_database_from_settings",
    "get_latest_completed_deployment_by_run_id",
    "get_latest_result_documents_by_run_ids",
    "load_documents_from_database",
    "load_latest_documents_by_run_ids",
]
