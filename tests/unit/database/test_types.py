"""Tests for canonical span-era database record types."""

import dataclasses
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from ai_pipeline_core.database._types import (
    _BlobRecord,
    CostTotals,
    DocumentRecord,
    HydratedDocument,
    LogRecord,
    SpanKind,
    SpanRecord,
    SpanStatus,
)


def test_span_kind_members() -> None:
    assert tuple(kind.value for kind in SpanKind) == (
        "deployment",
        "flow",
        "task",
        "attempt",
        "operation",
        "conversation",
        "prompt_execution",
        "llm_round",
        "tool_call",
    )


def test_span_status_members() -> None:
    assert tuple(status.value for status in SpanStatus) == (
        "running",
        "completed",
        "failed",
        "cached",
        "skipped",
    )


def test_span_record_defaults_and_immutability() -> None:
    deployment_id = uuid4()
    before = datetime.now(UTC)
    span = SpanRecord(
        span_id=uuid4(),
        parent_span_id=None,
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        run_id="run-123",
        deployment_name="example",
        kind=SpanKind.TASK,
        name="TaskName",
        status=SpanStatus.RUNNING,
        sequence_no=0,
        started_at=before,
        version=1,
    )

    assert span.description == ""
    assert span.ended_at is None
    assert span.cache_key == ""
    assert span.previous_conversation_id is None
    assert span.cost_usd == 0.0
    assert span.error_type == ""
    assert span.error_message == ""
    assert span.input_document_shas == ()
    assert span.output_document_shas == ()
    assert span.target == ""
    assert span.receiver_json == ""
    assert span.input_json == ""
    assert span.output_json == ""
    assert span.error_json == ""
    assert span.meta_json == ""
    assert span.metrics_json == ""
    assert span.input_blob_shas == ()
    assert span.output_blob_shas == ()
    assert not hasattr(span, "detail_json")

    with pytest.raises(dataclasses.FrozenInstanceError):
        span.name = "changed"  # type: ignore[misc]  # frozen model mutation negative test


def test_document_record_defaults_and_attachment_fields() -> None:
    record = DocumentRecord(
        document_sha256="doc-sha",
        content_sha256="blob-sha",
        document_type="ExampleDocument",
        name="example.md",
    )

    assert record.description == ""
    assert record.mime_type == ""
    assert record.size_bytes == 0
    assert record.summary == ""
    assert record.derived_from == ()
    assert record.triggered_by == ()
    assert record.attachment_names == ()
    assert record.attachment_descriptions == ()
    assert record.attachment_content_sha256s == ()
    assert record.attachment_mime_types == ()
    assert record.attachment_size_bytes == ()

    with pytest.raises(dataclasses.FrozenInstanceError):
        record.name = "changed"  # type: ignore[misc]  # frozen model mutation negative test


def test_document_record_rejects_mismatched_attachment_lengths() -> None:
    with pytest.raises(ValueError, match="matching lengths"):
        DocumentRecord(
            document_sha256="doc-sha",
            content_sha256="blob-sha",
            document_type="ExampleDocument",
            name="example.md",
            attachment_names=("a.txt",),
            attachment_descriptions=(),
            attachment_content_sha256s=("blob-a",),
            attachment_mime_types=("text/plain",),
            attachment_size_bytes=(1,),
        )


def test_blob_record_defaults_and_immutability() -> None:
    blob = _BlobRecord(content_sha256="blob-sha", content=b"hello")
    assert blob.content_sha256 == "blob-sha"
    assert blob.content == b"hello"

    with pytest.raises(dataclasses.FrozenInstanceError):
        blob.content = b"changed"  # type: ignore[misc]  # frozen model mutation negative test


def test_log_record_defaults_and_immutability() -> None:
    record = LogRecord(
        deployment_id=uuid4(),
        span_id=uuid4(),
        timestamp=datetime.now(UTC),
        sequence_no=3,
        level="INFO",
        category="framework",
        logger_name="tests.logger",
        message="hello",
    )

    assert record.event_type == ""
    assert record.fields_json == "{}"
    assert record.exception_text == ""

    with pytest.raises(dataclasses.FrozenInstanceError):
        record.message = "changed"  # type: ignore[misc]  # frozen model mutation negative test


def test_supporting_records_construct() -> None:
    totals = CostTotals(
        cost_usd=1.25,
        tokens_input=10,
        tokens_output=4,
        tokens_cache_read=3,
        tokens_reasoning=2,
    )
    hydrated = HydratedDocument(
        record=DocumentRecord(
            document_sha256="doc-sha",
            content_sha256="blob-sha",
            document_type="ExampleDocument",
            name="example.md",
        ),
        content=b"hello",
        attachment_contents={},
    )

    assert totals.cost_usd == 1.25
    assert totals.tokens_input + totals.tokens_output + totals.tokens_cache_read + totals.tokens_reasoning == 19
    assert hydrated.content == b"hello"
