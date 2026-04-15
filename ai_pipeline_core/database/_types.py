"""Data models for the span-based database schema."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from ai_pipeline_core.database._json_helpers import parse_json_object
from ai_pipeline_core.documents._context import DocumentSha256
from ai_pipeline_core.logger._types import LogRecord

__all__ = [
    "TOKENS_CACHE_READ_KEY",
    "TOKENS_INPUT_KEY",
    "TOKENS_OUTPUT_KEY",
    "TOKENS_REASONING_KEY",
    "CostTotals",
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
    "_BlobRecord",
    "aggregate_cost_totals",
    "get_token_count",
]

TOKENS_INPUT_KEY = "tokens_input"
TOKENS_OUTPUT_KEY = "tokens_output"
TOKENS_CACHE_READ_KEY = "tokens_cache_read"
TOKENS_REASONING_KEY = "tokens_reasoning"


def get_token_count(metrics: dict[str, Any], key: str) -> int:
    """Extract an integer token count from a metrics dict, tolerating float/string values."""
    value = metrics.get(key, 0)
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _validate_string_tuple(field_name: str, values: object) -> None:
    if not isinstance(values, tuple):
        msg = f"{field_name} must be a tuple[str, ...]. Pass a tuple like ('sha1', 'sha2'), not {type(values).__name__}."
        raise TypeError(msg)
    if not all(isinstance(value, str) for value in values):
        msg = f"{field_name} must contain only strings. Convert all values to str before constructing the record."
        raise TypeError(msg)


def _validate_int_tuple(field_name: str, values: object) -> None:
    if not isinstance(values, tuple):
        msg = f"{field_name} must be a tuple[int, ...]. Pass a tuple like (1, 2), not {type(values).__name__}."
        raise TypeError(msg)
    if not all(isinstance(value, int) for value in values):
        msg = f"{field_name} must contain only integers. Convert all values to int before constructing the record."
        raise TypeError(msg)


def _validate_bytes_mapping(field_name: str, values: object) -> None:
    if not isinstance(values, dict):
        msg = f"{field_name} must be a dict[str, bytes]. Pass a dict keyed by content_sha256."
        raise TypeError(msg)
    if not all(isinstance(key, str) for key in values):
        msg = f"{field_name} keys must be strings. Use attachment content SHA256 strings as keys."
        raise TypeError(msg)
    if not all(isinstance(value, bytes) for value in values.values()):
        msg = f"{field_name} values must be bytes. Load blob contents before constructing the hydrated document."
        raise TypeError(msg)


def _validate_enum_string(field_name: str, value: object, enum_type: type[StrEnum]) -> None:
    if not isinstance(value, str):
        msg = f"{field_name} must be a string. Pass one of {[item.value for item in enum_type]}."
        raise TypeError(msg)
    try:
        enum_type(value)
    except ValueError as exc:
        msg = f"{field_name} must be one of {[item.value for item in enum_type]}. Got {value!r}."
        raise ValueError(msg) from exc


# Enum
class SpanKind(StrEnum):
    """Discriminator for span-based execution records."""

    DEPLOYMENT = "deployment"
    FLOW = "flow"
    TASK = "task"
    ATTEMPT = "attempt"
    OPERATION = "operation"
    CONVERSATION = "conversation"
    LLM_ROUND = "llm_round"
    TOOL_CALL = "tool_call"


# Enum
class SpanStatus(StrEnum):
    """Lifecycle status for span-based execution records."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CACHED = "cached"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class SpanRecord:
    """Row from the span-oriented execution table."""

    span_id: UUID
    parent_span_id: UUID | None
    deployment_id: UUID
    root_deployment_id: UUID
    run_id: str
    kind: str
    name: str
    sequence_no: int
    deployment_name: str = ""
    description: str = ""
    status: str = SpanStatus.RUNNING
    started_at: datetime = field(default_factory=_utcnow)
    ended_at: datetime | None = None
    version: int = 1
    cache_key: str = ""
    previous_conversation_id: UUID | None = None
    cost_usd: float = 0.0
    error_type: str = ""
    error_message: str = ""
    input_document_shas: tuple[str, ...] = ()
    output_document_shas: tuple[str, ...] = ()
    target: str = ""
    receiver_json: str = ""
    input_json: str = ""
    output_json: str = ""
    error_json: str = ""
    meta_json: str = ""
    metrics_json: str = ""
    input_blob_shas: tuple[str, ...] = ()
    output_blob_shas: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_enum_string("kind", self.kind, SpanKind)
        _validate_enum_string("status", self.status, SpanStatus)
        _validate_string_tuple("input_document_shas", self.input_document_shas)
        _validate_string_tuple("output_document_shas", self.output_document_shas)
        _validate_string_tuple("input_blob_shas", self.input_blob_shas)
        _validate_string_tuple("output_blob_shas", self.output_blob_shas)


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    """Row from the content-addressed documents table."""

    document_sha256: DocumentSha256
    content_sha256: str
    document_type: str
    name: str
    description: str = ""
    mime_type: str = ""
    size_bytes: int = 0
    summary: str = ""
    derived_from: tuple[str, ...] = ()
    triggered_by: tuple[str, ...] = ()
    attachment_names: tuple[str, ...] = ()
    attachment_descriptions: tuple[str, ...] = ()
    attachment_content_sha256s: tuple[str, ...] = ()
    attachment_mime_types: tuple[str, ...] = ()
    attachment_size_bytes: tuple[int, ...] = ()
    publicly_visible: bool = False

    def __post_init__(self) -> None:
        _validate_string_tuple("derived_from", self.derived_from)
        _validate_string_tuple("triggered_by", self.triggered_by)
        _validate_string_tuple("attachment_names", self.attachment_names)
        _validate_string_tuple("attachment_descriptions", self.attachment_descriptions)
        _validate_string_tuple("attachment_content_sha256s", self.attachment_content_sha256s)
        _validate_string_tuple("attachment_mime_types", self.attachment_mime_types)
        _validate_int_tuple("attachment_size_bytes", self.attachment_size_bytes)
        attachment_count = len(self.attachment_names)
        attachment_lengths = (
            len(self.attachment_descriptions),
            len(self.attachment_content_sha256s),
            len(self.attachment_mime_types),
            len(self.attachment_size_bytes),
        )
        if any(length != attachment_count for length in attachment_lengths):
            msg = (
                "DocumentRecord attachment fields must have matching lengths. "
                "Provide one name, description, content_sha256, mime_type, and size_bytes entry for each attachment."
            )
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class _BlobRecord:
    """Row from the immutable blobs table."""

    content_sha256: str
    content: bytes


@dataclass(frozen=True, slots=True)
class CostTotals:
    """Aggregated cost and token totals for a deployment. Cost includes all span kinds, token counts include llm_round spans only."""

    cost_usd: float = 0.0
    tokens_input: int = 0
    tokens_output: int = 0
    tokens_cache_read: int = 0
    tokens_reasoning: int = 0


@dataclass(frozen=True, slots=True)
class HydratedDocument:
    """Document metadata with loaded primary and attachment blob content."""

    record: DocumentRecord
    content: bytes
    attachment_contents: dict[str, bytes] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_bytes_mapping("attachment_contents", self.attachment_contents)


@dataclass(frozen=True, slots=True)
class DeploymentSummaryRecord:
    """Lightweight deployment record for list/summary views."""

    deployment_id: UUID
    root_deployment_id: UUID
    run_id: str
    deployment_name: str
    name: str
    status: str
    started_at: datetime
    ended_at: datetime | None
    parent_span_id: UUID | None
    cost_usd: float


@dataclass(frozen=True, slots=True)
class DocumentProducerRecord:
    """Maps a document to the span that first produced it."""

    document_sha256: str
    span_id: UUID
    span_name: str
    span_kind: str
    deployment_id: UUID
    deployment_name: str
    started_at: datetime


@dataclass(frozen=True, slots=True)
class DocumentEventRecord:
    """Lightweight record for document input/output events."""

    document_sha256: str
    span_id: UUID
    span_name: str
    span_kind: str
    deployment_id: UUID
    timestamp: datetime
    direction: str  # "input" | "output"


@dataclass(frozen=True, slots=True)
class DocumentEventPage:
    """Filtered document events plus total count."""

    events: tuple[DocumentEventRecord, ...]
    total_events: int


def aggregate_cost_totals(items: Iterable[tuple[str, float, str, str]]) -> CostTotals:
    cost_usd = 0.0
    tokens_input = 0
    tokens_output = 0
    tokens_cache_read = 0
    tokens_reasoning = 0
    for kind, span_cost_usd, metrics_json, context in items:
        cost_usd += float(span_cost_usd)
        if kind != SpanKind.LLM_ROUND:
            continue
        metrics = parse_json_object(metrics_json, context=context, field_name="metrics_json")
        tokens_input += get_token_count(metrics, TOKENS_INPUT_KEY)
        tokens_output += get_token_count(metrics, TOKENS_OUTPUT_KEY)
        tokens_cache_read += get_token_count(metrics, TOKENS_CACHE_READ_KEY)
        tokens_reasoning += get_token_count(metrics, TOKENS_REASONING_KEY)
    return CostTotals(
        cost_usd=cost_usd,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        tokens_cache_read=tokens_cache_read,
        tokens_reasoning=tokens_reasoning,
    )
