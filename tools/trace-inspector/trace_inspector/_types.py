"""Shared types and helper functions for trace-inspector."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_pipeline_core.database._protocol import DatabaseReader
from ai_pipeline_core.database._types import DocumentRecord, SpanRecord


# Protocol
@runtime_checkable
class TraceReader(DatabaseReader, Protocol):
    """Database reader that also supports shutdown for connection cleanup."""

    async def shutdown(self) -> None: ...


__all__ = [
    "ComparisonSelection",
    "LoadedAttachment",
    "LoadedDocument",
    "LoadedTask",
    "LoadedTrace",
    "ProvenanceEntry",
    "ReaderConnection",
    "RenderConfig",
    "Selection",
    "SourceSpec",
    "document_filename",
    "is_textual_mime_type",
    "make_short_id_map",
    "sanitize_segment",
    "snake_case_segment",
]

DEFAULT_SHORT_ID_LENGTH = 8
MAX_SEGMENT_LENGTH = 80

DEFAULT_INLINE_DOCUMENT_BYTES = 5 * 1024
DEFAULT_HEAD_CHARS = 500
DEFAULT_TAIL_CHARS = 500
DEFAULT_COMPACT_HEAD_CHARS = 200
DEFAULT_COMPACT_TAIL_CHARS = 200
DEFAULT_COMPACT_LARGE_DOCUMENT_THRESHOLD = 10
DEFAULT_BATCH_THRESHOLD = 5
_UNSAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9._-]+")
_CAMEL_BOUNDARY_RE = re.compile(r"(?<!^)(?=[A-Z])")


class _FrozenModel(BaseModel):
    """Frozen base model for internal trace-inspector data."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)


class SourceSpec(_FrozenModel):
    """Database source selection for one trace load."""

    db_path: Path | None = None
    run_id: str | None = None
    deployment_id: UUID | None = None
    download_db_to: Path | None = None

    @model_validator(mode="after")
    def _validate_source(self) -> SourceSpec:
        configured_sources = [self.db_path is not None, self.run_id is not None, self.deployment_id is not None]
        if sum(configured_sources) != 1:
            msg = (
                "Trace inspector source selection requires exactly one of db_path, run_id, or deployment_id. "
                "Pass a FilesystemDatabase path for local inspection, or a run/deployment identifier for ClickHouse-backed inspection."
            )
            raise ValueError(msg)
        if self.db_path is not None and self.download_db_to is not None:
            msg = (
                "download_db_to is only supported with ClickHouse sources (run_id or deployment_id). "
                "Remove download_db_to when using db_path — the FilesystemDatabase is already local."
            )
            raise ValueError(msg)
        return self


class RenderConfig(_FrozenModel):
    """Output rendering configuration for markdown bundle generation."""

    inline_document_bytes: int = Field(default=DEFAULT_INLINE_DOCUMENT_BYTES)
    head_chars: int = Field(default=DEFAULT_HEAD_CHARS)
    tail_chars: int = Field(default=DEFAULT_TAIL_CHARS)
    compact_head_chars: int = Field(default=DEFAULT_COMPACT_HEAD_CHARS)
    compact_tail_chars: int = Field(default=DEFAULT_COMPACT_TAIL_CHARS)
    compact_large_document_threshold: int = Field(default=DEFAULT_COMPACT_LARGE_DOCUMENT_THRESHOLD)
    batch_threshold: int = Field(default=DEFAULT_BATCH_THRESHOLD)


class Selection(_FrozenModel):
    """Inspection selection for one output bundle."""

    mode: Literal["run", "flow", "task"] = "run"
    source: SourceSpec
    output_dir: Path
    render_config: RenderConfig = Field(default_factory=RenderConfig)
    failed_only: bool = False
    flow_name: str | None = None
    flow_span_id: UUID | None = None
    task_span_id: UUID | None = None


class ComparisonSelection(_FrozenModel):
    """Selection for original-vs-replay comparison output."""

    left_source: SourceSpec
    right_source: SourceSpec
    output_dir: Path
    left_task_span_id: UUID
    right_task_span_id: UUID | None = None
    render_config: RenderConfig = Field(default_factory=RenderConfig)


class ReaderConnection(_FrozenModel):
    """Opened database reader plus resolved deployment id."""

    reader: TraceReader
    deployment_id: UUID
    reader_label: str


class LoadedAttachment(_FrozenModel):
    """Hydrated attachment content for a loaded document."""

    name: str
    mime_type: str
    size_bytes: int
    content_sha256: str
    content: bytes | None
    text_content: str | None


class LoadedDocument(_FrozenModel):
    """Hydrated document content and canonical output identity."""

    record: DocumentRecord
    short_id: str
    output_filename: str
    content: bytes | None
    text_content: str | None
    attachments: tuple[LoadedAttachment, ...]


class LoadedTask(_FrozenModel):
    """Task span plus related execution descendants used during rendering."""

    span: SpanRecord
    parent_flow_span: SpanRecord
    attempt_spans: tuple[SpanRecord, ...]
    conversation_spans: tuple[SpanRecord, ...]
    llm_rounds_by_conversation: dict[UUID, tuple[SpanRecord, ...]]
    tool_calls_by_conversation: dict[UUID, tuple[SpanRecord, ...]]
    input_document_shas: tuple[str, ...]
    output_document_shas: tuple[str, ...]
    descendant_cost_usd: float = 0.0
    duration_seconds: float | None = None
    tokens_input: int = 0
    tokens_output: int = 0
    tokens_cache_read: int = 0
    tokens_reasoning: int = 0
    parent_task_id: UUID | None = None
    child_task_ids: tuple[UUID, ...] = ()


class LoadedTrace(_FrozenModel):
    """Complete loaded trace graph from one deployment tree."""

    root_span: SpanRecord
    spans_by_id: dict[UUID, SpanRecord]
    children_map: dict[UUID | None, tuple[UUID, ...]]
    flow_spans: tuple[SpanRecord, ...]
    tasks_by_flow: dict[UUID, tuple[UUID, ...]]
    tasks: dict[UUID, LoadedTask]
    documents: dict[str, LoadedDocument]
    reader_label: str = ""


class ProvenanceEntry(_FrozenModel):
    """Producer/consumer/provenance information for one document."""

    document_sha256: str
    produced_by_task_id: UUID | None
    produced_by_label: str
    consumed_by_task_ids: tuple[UUID, ...]
    consumed_by_labels: tuple[str, ...]
    derived_from_labels: tuple[str, ...]
    triggered_by_labels: tuple[str, ...]
    external_source_labels: tuple[str, ...]


def sanitize_segment(value: str) -> str:
    """Return a filesystem-safe segment for generated markdown paths."""
    cleaned = _UNSAFE_SEGMENT_RE.sub("-", value).strip("-._")
    if not cleaned:
        return "unnamed"
    return cleaned[:MAX_SEGMENT_LENGTH]


def snake_case_segment(value: str) -> str:
    """Return a filesystem-safe snake_case segment for generated bundle paths."""
    spaced = _CAMEL_BOUNDARY_RE.sub("_", value).replace("-", "_").replace(" ", "_")
    cleaned = re.sub(r"_+", "_", _UNSAFE_SEGMENT_RE.sub("_", spaced)).strip("_.").lower()
    if not cleaned:
        return "unnamed"
    return cleaned[:MAX_SEGMENT_LENGTH]


def make_short_id_map(document_shas: list[str], *, base_length: int = DEFAULT_SHORT_ID_LENGTH) -> dict[str, str]:
    """Create collision-safe short ids derived from document SHA256 prefixes."""
    unique_shas = sorted(set(document_shas))
    short_ids: dict[str, str] = {}
    taken: set[str] = set()
    for sha in unique_shas:
        length = base_length
        short_id = sha[:length]
        while short_id in taken:
            length += 1
            short_id = sha[:length]
        short_ids[sha] = short_id
        taken.add(short_id)
    return short_ids


def document_filename(short_id: str) -> str:
    """Return the canonical markdown filename for a short document id."""
    return f"{short_id}.md"


def is_textual_mime_type(mime_type: str) -> bool:
    """Return whether the mime type should be rendered as text."""
    return mime_type.startswith("text/") or mime_type in {
        "application/json",
        "application/xml",
        "application/yaml",
        "application/x-yaml",
        "application/javascript",
    }
