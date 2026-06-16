"""Shared serialization helpers for span and log records.

Column tuples and row converters used by both the ClickHouse and Filesystem backends.
ClickHouse-specific helpers (document/blob rows, INSERT row builders) stay in clickhouse/_rows.py.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from ai_pipeline_core.database._types import SpanRecord
from ai_pipeline_core.logger._types import LogRecord

__all__ = [
    "LOG_COLUMNS",
    "SPAN_COLUMNS",
    "decode_text",
    "row_to_log",
    "row_to_span",
    "string_tuple",
    "to_datetime",
    "to_uuid",
]

SPAN_COLUMNS = (
    "span_id",
    "parent_span_id",
    "deployment_id",
    "root_deployment_id",
    "run_id",
    "deployment_name",
    "kind",
    "name",
    "description",
    "status",
    "sequence_no",
    "started_at",
    "ended_at",
    "version",
    "cache_key",
    "previous_conversation_id",
    "cost_usd",
    "error_type",
    "error_message",
    "input_document_shas",
    "output_document_shas",
    "target",
    "receiver_json",
    "input_json",
    "output_json",
    "error_json",
    "meta_json",
    "metrics_json",
    "input_blob_shas",
    "output_blob_shas",
    "label_keys",
    "label_values",
)

LOG_COLUMNS = (
    "deployment_id",
    "span_id",
    "timestamp",
    "sequence_no",
    "level",
    "category",
    "event_type",
    "logger_name",
    "message",
    "fields_json",
    "exception_text",
)


def decode_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def to_uuid(value: Any) -> UUID:
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def to_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(decode_text(item) for item in value)
    return (decode_text(value),)


def row_to_span(row: tuple[Any, ...]) -> SpanRecord:
    fields = dict(zip(SPAN_COLUMNS, row, strict=True))
    fields["span_id"] = to_uuid(fields["span_id"])
    parent_span_id = fields["parent_span_id"]
    fields["parent_span_id"] = None if parent_span_id is None else to_uuid(parent_span_id)
    fields["deployment_id"] = to_uuid(fields["deployment_id"])
    fields["root_deployment_id"] = to_uuid(fields["root_deployment_id"])
    previous_conversation_id = fields["previous_conversation_id"]
    fields["previous_conversation_id"] = None if previous_conversation_id is None else to_uuid(previous_conversation_id)
    fields["run_id"] = decode_text(fields["run_id"])
    fields["deployment_name"] = decode_text(fields["deployment_name"])
    fields["kind"] = decode_text(fields["kind"])
    fields["name"] = decode_text(fields["name"])
    fields["description"] = decode_text(fields["description"])
    fields["status"] = decode_text(fields["status"])
    fields["sequence_no"] = int(fields["sequence_no"])
    fields["started_at"] = to_datetime(fields["started_at"])
    ended_at = fields["ended_at"]
    fields["ended_at"] = None if ended_at is None else to_datetime(ended_at)
    fields["version"] = int(fields["version"])
    fields["cache_key"] = decode_text(fields["cache_key"])
    fields["cost_usd"] = float(fields["cost_usd"])
    fields["error_type"] = decode_text(fields["error_type"])
    fields["error_message"] = decode_text(fields["error_message"])
    fields["input_document_shas"] = string_tuple(fields["input_document_shas"])
    fields["output_document_shas"] = string_tuple(fields["output_document_shas"])
    fields["target"] = decode_text(fields["target"])
    fields["receiver_json"] = decode_text(fields["receiver_json"])
    fields["input_json"] = decode_text(fields["input_json"])
    fields["output_json"] = decode_text(fields["output_json"])
    fields["error_json"] = decode_text(fields["error_json"])
    fields["meta_json"] = decode_text(fields["meta_json"])
    fields["metrics_json"] = decode_text(fields["metrics_json"])
    fields["input_blob_shas"] = string_tuple(fields["input_blob_shas"])
    fields["output_blob_shas"] = string_tuple(fields["output_blob_shas"])
    fields["label_keys"] = string_tuple(fields["label_keys"])
    fields["label_values"] = string_tuple(fields["label_values"])
    return SpanRecord(**fields)


def row_to_log(row: tuple[Any, ...]) -> LogRecord:
    fields = dict(zip(LOG_COLUMNS, row, strict=True))
    fields["deployment_id"] = to_uuid(fields["deployment_id"])
    fields["span_id"] = to_uuid(fields["span_id"])
    fields["timestamp"] = to_datetime(fields["timestamp"])
    fields["sequence_no"] = int(fields["sequence_no"])
    fields["level"] = decode_text(fields["level"])
    fields["category"] = decode_text(fields["category"])
    fields["event_type"] = decode_text(fields["event_type"])
    fields["logger_name"] = decode_text(fields["logger_name"])
    fields["message"] = decode_text(fields["message"])
    fields["fields_json"] = decode_text(fields["fields_json"]) or "{}"
    fields["exception_text"] = decode_text(fields["exception_text"])
    return LogRecord(**fields)
