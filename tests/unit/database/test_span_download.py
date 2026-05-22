"""Tests for span-era deployment snapshot export."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from ai_pipeline_core.database import DocumentRecord, LogRecord, SpanKind, SpanRecord, SpanStatus
from ai_pipeline_core.database.filesystem._backend import FilesystemDatabase
from ai_pipeline_core.database.filesystem._validation import validate_bundle
from ai_pipeline_core.database.snapshot._download import download_deployment
from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.database._types import _BlobRecord


def _make_span(**kwargs: object) -> SpanRecord:
    deployment_id = kwargs.pop("deployment_id", uuid4())
    root_deployment_id = kwargs.pop("root_deployment_id", deployment_id)
    started_at: datetime = kwargs.pop("started_at", datetime(2026, 3, 11, 12, 0, tzinfo=UTC))
    defaults: dict[str, object] = {
        "span_id": kwargs.pop("span_id", uuid4()),
        "parent_span_id": kwargs.pop("parent_span_id", None),
        "deployment_id": deployment_id,
        "root_deployment_id": root_deployment_id,
        "run_id": kwargs.pop("run_id", f"run-{deployment_id}"),
        "deployment_name": "download-deployment",
        "kind": SpanKind.TASK,
        "name": "ExampleTask",
        "status": SpanStatus.COMPLETED,
        "sequence_no": 0,
        "started_at": started_at,
        "ended_at": started_at + timedelta(seconds=1),
        "version": 1,
        "meta_json": "",
        "metrics_json": "",
    }
    defaults.update(kwargs)
    return SpanRecord(**defaults)


def _make_document(**kwargs: object) -> DocumentRecord:
    defaults: dict[str, object] = {
        "document_sha256": f"doc-{uuid4().hex}",
        "content_sha256": f"blob-{uuid4().hex}",
        "document_type": "TestDocument",
        "name": "example.md",
        "description": "Example document",
        "mime_type": "text/markdown",
        "size_bytes": 20,
        "summary": "",
        "derived_from": (),
        "triggered_by": (),
        "attachment_names": (),
        "attachment_descriptions": (),
        "attachment_content_sha256s": (),
        "attachment_mime_types": (),
        "attachment_size_bytes": (),
    }
    defaults.update(kwargs)
    return DocumentRecord(**defaults)


def _make_blob(**kwargs: object) -> _BlobRecord:
    defaults: dict[str, object] = {
        "content_sha256": f"blob-{uuid4().hex}",
        "content": b"blob-content",
    }
    defaults.update(kwargs)
    return _BlobRecord(**defaults)


def _make_log(**kwargs: object) -> LogRecord:
    defaults: dict[str, object] = {
        "deployment_id": uuid4(),
        "span_id": uuid4(),
        "timestamp": datetime(2026, 3, 11, 12, 0, tzinfo=UTC),
        "sequence_no": 0,
        "level": "INFO",
        "category": "framework",
        "event_type": "",
        "logger_name": "ai_pipeline_core.tests",
        "message": "log message",
        "fields_json": "{}",
        "exception_text": "",
    }
    defaults.update(kwargs)
    return LogRecord(**defaults)


async def _seed_source_database() -> tuple[_MemoryDatabase, UUID]:
    source = _MemoryDatabase()
    deployment_id = uuid4()
    base = datetime(2026, 3, 11, 12, 0, tzinfo=UTC)

    deployment = _make_span(
        span_id=deployment_id,
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        kind=SpanKind.DEPLOYMENT,
        name="RootDeployment",
        started_at=base,
    )
    task = _make_span(
        parent_span_id=deployment.span_id,
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        kind=SpanKind.TASK,
        name="RootTask",
        sequence_no=1,
        started_at=base + timedelta(seconds=5),
        output_document_shas=("doc-root",),
    )
    llm_round = _make_span(
        parent_span_id=task.span_id,
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        kind=SpanKind.LLM_ROUND,
        name="round-1",
        sequence_no=1,
        started_at=base + timedelta(seconds=6),
        cost_usd=0.5,
        meta_json=json.dumps(
            {
                "model": "test-model",
                "round_index": 1,
                "request_messages": [{"role": "user", "content": "hello"}],
                "response_content": "final answer",
                "response_tool_calls": [],
            },
            sort_keys=True,
        ),
        metrics_json=json.dumps(
            {
                "tokens_input": 100,
                "tokens_output": 25,
                "tokens_cache_read": 10,
                "tokens_reasoning": 5,
            },
            sort_keys=True,
        ),
    )

    for span in (deployment, task, llm_round):
        await source.insert_span(span)

    await source.save_document(
        _make_document(
            document_sha256="doc-root",
            content_sha256="blob-root",
            document_type="RootDoc",
            name="root.md",
            description="Root document",
            mime_type="text/markdown",
            size_bytes=10,
            attachment_names=("shared.png",),
            attachment_descriptions=("shared attachment",),
            attachment_content_sha256s=("blob-shared-attachment",),
            attachment_mime_types=("image/png",),
            attachment_size_bytes=(5,),
        )
    )
    await source.save_blob(_make_blob(content_sha256="blob-root", content=b"shared"))
    await source.save_blob(_make_blob(content_sha256="blob-shared-attachment", content=b"attachment"))
    await source.save_logs_batch([
        _make_log(
            deployment_id=deployment_id,
            span_id=task.span_id,
            timestamp=base + timedelta(seconds=7),
            message="root log",
        )
    ])

    return source, deployment_id


@pytest.mark.asyncio
async def test_download_deployment_writes_portable_snapshot(tmp_path: Path) -> None:
    source, deployment_id = await _seed_source_database()
    output_path = tmp_path / "snapshot"

    await download_deployment(source, deployment_id, output_path)

    snapshot = FilesystemDatabase(output_path, read_only=True)
    hydrated = await snapshot.get_document_with_content("doc-root")
    deployment = await snapshot.get_deployment_by_run_id(f"run-{deployment_id}")

    assert deployment is not None
    assert hydrated is not None
    assert hydrated.content == b"shared"
    assert hydrated.attachment_contents == {"blob-shared-attachment": b"attachment"}


@pytest.mark.asyncio
async def test_download_deployment_bundle_validation_reads_attachment_fields(tmp_path: Path) -> None:
    source, deployment_id = await _seed_source_database()
    output_path = tmp_path / "snapshot"

    await download_deployment(source, deployment_id, output_path)
    validation = validate_bundle(output_path)

    assert validation["valid"] is True
    assert validation["document_count"] == 1
    assert validation["blob_count"] >= 2


def test_validate_bundle_requires_schema_meta(tmp_path: Path) -> None:
    """A snapshot bundle must contain schema_meta.json for provenance tracking."""
    (tmp_path / "runs").mkdir()
    (tmp_path / "documents").mkdir()
    (tmp_path / "blobs").mkdir()
    (tmp_path / "logs.jsonl").write_text("", encoding="utf-8")

    with pytest.raises((FileNotFoundError, ValueError), match="schema_meta"):
        validate_bundle(tmp_path)
