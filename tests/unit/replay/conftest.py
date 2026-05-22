"""Shared fixtures and helpers for generic replay tests."""

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
import json
import struct
import zlib
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ConfigDict

from ai_pipeline_core._codec import UniversalCodec
from ai_pipeline_core.database import DocumentRecord, SpanKind, SpanRecord, SpanStatus
from ai_pipeline_core.database._documents import document_to_blobs, document_to_record
from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.documents import Attachment, Document
from ai_pipeline_core.pipeline.options import FlowOptions

_UNSET = object()


@pytest.fixture(autouse=True)
def _suppress_registration() -> Generator[None]:
    return


class ReplayTextDocument(Document):
    """Text document used by replay tests."""


class ReplayAttachmentDocument(Document):
    """Document with attachments used by replay tests."""


class ReplayResultDocument(Document):
    """Output document returned from replay test tasks."""


class ReplayFlowOptions(FlowOptions):
    """Flow options used by replay tests."""

    replay_label: str = "baseline"


class ReplayPayload(BaseModel):
    """Frozen payload model used by replay tests."""

    model_config = ConfigDict(frozen=True)

    label: str


def make_test_png_bytes() -> bytes:
    """Return a valid minimal 1x1 red PNG."""
    png_header = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    ihdr_crc = zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF
    ihdr = struct.pack(">I", 13) + b"IHDR" + ihdr_data + struct.pack(">I", ihdr_crc)
    raw_data = zlib.compress(b"\x00\xff\x00\x00")
    idat_crc = zlib.crc32(b"IDAT" + raw_data) & 0xFFFFFFFF
    idat = struct.pack(">I", len(raw_data)) + b"IDAT" + raw_data + struct.pack(">I", idat_crc)
    iend_crc = zlib.crc32(b"IEND") & 0xFFFFFFFF
    iend = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", iend_crc)
    return png_header + ihdr + idat + iend


async def store_document_in_database(
    database: _MemoryDatabase,
    doc: Document,
) -> DocumentRecord:
    """Persist a document and its blobs into the in-memory database."""
    record = document_to_record(doc)
    blobs = document_to_blobs(doc)
    await database.save_document(record)
    await database.save_blob_batch(blobs)
    return record


def _json_dumps(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def make_span(
    *,
    kind: str = SpanKind.TASK,
    name: str = "ReplaySpan",
    run_id: str = "replay-run",
    deployment_id: UUID | None = None,
    root_deployment_id: UUID | None = None,
    parent_span_id: UUID | None = None,
    sequence_no: int = 0,
    status: str = SpanStatus.COMPLETED,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    target: str = "",
    receiver_mode: str | None = None,
    receiver_value: object = _UNSET,
    input_value: object = _UNSET,
    meta: dict[str, object] | None = None,
    metrics: dict[str, object] | None = None,
    input_document_shas: tuple[str, ...] | None = None,
    input_blob_shas: tuple[str, ...] | None = None,
    previous_conversation_id: UUID | None = None,
    cost_usd: float = 0.0,
) -> SpanRecord:
    codec = UniversalCodec()
    deployment_uuid = deployment_id or uuid4()
    root_uuid = root_deployment_id or deployment_uuid
    started = started_at or datetime(2026, 3, 13, 12, 0, tzinfo=UTC)
    finished = ended_at
    if finished is None and status != SpanStatus.RUNNING:
        finished = started + timedelta(seconds=1)

    receiver_json = ""
    receiver_document_shas: frozenset[str] = frozenset()
    receiver_blob_shas: frozenset[str] = frozenset()
    if receiver_mode is not None:
        encoded_receiver = codec.encode(None if receiver_value is _UNSET else receiver_value)
        receiver_json = _json_dumps({"mode": receiver_mode, "value": encoded_receiver.value})
        receiver_document_shas = encoded_receiver.document_shas
        receiver_blob_shas = encoded_receiver.blob_shas

    input_json = ""
    input_document_refs: frozenset[str] = frozenset()
    input_blob_refs: frozenset[str] = frozenset()
    if input_value is not _UNSET:
        encoded_input = codec.encode(input_value)
        input_json = _json_dumps(encoded_input.value)
        input_document_refs = encoded_input.document_shas
        input_blob_refs = encoded_input.blob_shas

    return SpanRecord(
        span_id=uuid4(),
        parent_span_id=parent_span_id,
        deployment_id=deployment_uuid,
        root_deployment_id=root_uuid,
        run_id=run_id,
        deployment_name="replay-tests",
        kind=kind,
        name=name,
        sequence_no=sequence_no,
        status=status,
        started_at=started,
        ended_at=finished,
        version=1,
        previous_conversation_id=previous_conversation_id,
        input_document_shas=tuple(sorted(input_document_shas or tuple((*receiver_document_shas, *input_document_refs)))),
        output_document_shas=(),
        input_blob_shas=tuple(sorted(input_blob_shas or tuple((*receiver_blob_shas, *input_blob_refs)))),
        output_blob_shas=(),
        cost_usd=cost_usd,
        target=target,
        receiver_json=receiver_json,
        input_json=input_json,
        output_json="",
        error_json="",
        meta_json=_json_dumps(meta or {}),
        metrics_json=_json_dumps(metrics or {}),
    )


@pytest.fixture
def memory_database() -> _MemoryDatabase:
    return _MemoryDatabase()


@pytest.fixture
def sample_text_doc() -> ReplayTextDocument:
    return ReplayTextDocument(
        name="notes.txt",
        content=b"Replay fixture text document with some content for testing.",
        description="Text fixture for replay tests",
    )


@pytest.fixture
def sample_attachment_doc() -> ReplayAttachmentDocument:
    return ReplayAttachmentDocument(
        name="bundle.md",
        content=b"# Bundle\nMain content for replay fixture.",
        description="Attachment fixture for replay tests",
        attachments=(
            Attachment(
                name="details.txt",
                content="Attachment text content",
                description="Text attachment",
            ),
            Attachment(
                name="preview.png",
                content=make_test_png_bytes(),
                description="Image attachment",
            ),
        ),
    )
