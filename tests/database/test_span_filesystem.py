"""Tests for the span-oriented filesystem database backend."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from ai_pipeline_core.database import DocumentRecord, LogRecord, SpanKind, SpanRecord, SpanStatus
from ai_pipeline_core.database._types import _BlobRecord
from ai_pipeline_core.database.filesystem._backend import FilesystemDatabase


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
        "deployment_name": "example",
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


async def _seed_database(tmp_path: Path) -> tuple[FilesystemDatabase, str]:
    database = FilesystemDatabase(tmp_path)
    deployment_id = uuid4()
    base = datetime(2026, 3, 11, 12, 0, tzinfo=UTC)

    deployment = _make_span(
        span_id=deployment_id,
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        kind=SpanKind.DEPLOYMENT,
        name="Deployment",
        started_at=base,
    )
    task = _make_span(
        parent_span_id=deployment.span_id,
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        kind=SpanKind.TASK,
        name="Task",
        sequence_no=1,
        started_at=base + timedelta(seconds=5),
        output_document_shas=("doc-1",),
    )
    llm_round = _make_span(
        parent_span_id=task.span_id,
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        kind=SpanKind.LLM_ROUND,
        name="Round1",
        sequence_no=1,
        started_at=base + timedelta(seconds=6),
        cost_usd=2.0,
        metrics_json=json.dumps(
            {
                "tokens_input": 50,
                "tokens_output": 10,
                "tokens_cache_read": 5,
                "tokens_reasoning": 1,
            },
            sort_keys=True,
        ),
    )

    for span in (deployment, task, llm_round):
        await database.insert_span(span)

    await database.save_document(
        _make_document(
            document_sha256="doc-1",
            content_sha256="blob-1",
            attachment_names=("preview.png",),
            attachment_descriptions=("Preview",),
            attachment_content_sha256s=("blob-preview",),
            attachment_mime_types=("image/png",),
            attachment_size_bytes=(7,),
        )
    )
    await database.save_blob(_make_blob(content_sha256="blob-1", content=b"root"))
    await database.save_blob(_make_blob(content_sha256="blob-preview", content=b"pngdata"))
    await database.save_logs_batch([
        _make_log(
            deployment_id=deployment_id,
            span_id=task.span_id,
            timestamp=base + timedelta(seconds=7),
            message="task log",
        )
    ])
    await database.flush()
    return database, "doc-1"


@pytest.mark.asyncio
async def test_filesystem_database_persists_and_reloads_records(tmp_path: Path) -> None:
    database, document_sha = await _seed_database(tmp_path)

    hydrated = await database.get_document_with_content(document_sha)
    logs = await database.get_deployment_logs(next(iter(database._spans.values())).deployment_id)

    assert hydrated is not None
    assert hydrated.content == b"root"
    assert hydrated.attachment_contents == {"blob-preview": b"pngdata"}
    assert [log.message for log in logs] == ["task log"]

    reloaded = FilesystemDatabase(tmp_path, read_only=True)
    reloaded_hydrated = await reloaded.get_document_with_content(document_sha)
    totals = await reloaded.get_deployment_cost_totals(next(iter(reloaded._spans.values())).root_deployment_id)

    assert reloaded_hydrated is not None
    assert reloaded_hydrated.record.attachment_names == ("preview.png",)
    assert totals.cost_usd == 2.0
    assert totals.tokens_input == 50


@pytest.mark.asyncio
async def test_filesystem_topology_and_activity_helpers_match_tree_shape(tmp_path: Path) -> None:
    database, _document_sha = await _seed_database(tmp_path)
    root_id = next(span.root_deployment_id for span in database._spans.values())

    topology = await database.get_deployment_tree_topology(root_id)
    tree = await database.get_deployment_tree(root_id)
    latest_activity = await database.get_deployment_latest_activity(root_id)
    reaper_activity = await database.latest_span_activity_for_deployment(root_id)

    assert topology == tree
    assert latest_activity == reaper_activity
    assert latest_activity == max(span.ended_at or span.started_at for span in tree)


@pytest.mark.asyncio
async def test_filesystem_document_insert_once_and_summary_update(tmp_path: Path) -> None:
    database = FilesystemDatabase(tmp_path)
    first = _make_document(document_sha256="doc-1", summary="first")
    second = _make_document(document_sha256="doc-1", summary="second")

    await database.save_document(first)
    await database.save_document(second)
    stored = await database.get_document("doc-1")
    assert stored is not None
    assert stored.summary == "first"

    await database.update_document_summary("doc-1", "updated")
    updated = await database.get_document("doc-1")

    assert updated is not None
    assert updated.summary == "updated"
    assert updated.name == first.name


@pytest.mark.asyncio
async def test_filesystem_get_document_with_content_raises_for_missing_attachment_blob(tmp_path: Path) -> None:
    database = FilesystemDatabase(tmp_path)
    await database.save_document(
        _make_document(
            document_sha256="doc-1",
            content_sha256="blob-1",
            attachment_names=("preview.png",),
            attachment_descriptions=("Preview",),
            attachment_content_sha256s=("blob-missing",),
            attachment_mime_types=("image/png",),
            attachment_size_bytes=(10,),
        )
    )
    await database.save_blob(_make_blob(content_sha256="blob-1", content=b"root"))

    with pytest.raises(ValueError, match="missing from storage"):
        await database.get_document_with_content("doc-1")


@pytest.mark.asyncio
async def test_filesystem_read_only_rejects_writes(tmp_path: Path) -> None:
    database, _ = await _seed_database(tmp_path)
    await database.shutdown()

    read_only = FilesystemDatabase(tmp_path, read_only=True)

    with pytest.raises(PermissionError, match="read-only"):
        await read_only.save_document(_make_document())


@pytest.mark.asyncio
async def test_filesystem_find_documents_by_name(tmp_path: Path) -> None:
    database = FilesystemDatabase(tmp_path)
    doc_a = _make_document(document_sha256="aaa", name="report.md", document_type="Report")
    doc_b = _make_document(document_sha256="bbb", name="summary.md", document_type="Summary")
    doc_c = _make_document(document_sha256="ccc", name="other.md", document_type="Report")
    for doc in (doc_a, doc_b, doc_c):
        await database.save_document(doc)

    found = await database.find_documents_by_name(["report.md", "summary.md"])

    assert set(found.keys()) == {"report.md", "summary.md"}
    assert found["report.md"].document_sha256 == "aaa"
    assert found["summary.md"].document_sha256 == "bbb"


@pytest.mark.asyncio
async def test_filesystem_find_documents_by_name_filters_by_type(tmp_path: Path) -> None:
    database = FilesystemDatabase(tmp_path)
    doc_a = _make_document(document_sha256="aaa", name="report.md", document_type="Report")
    doc_b = _make_document(document_sha256="bbb", name="report.md", document_type="Draft")
    for doc in (doc_a, doc_b):
        await database.save_document(doc)

    found = await database.find_documents_by_name(["report.md"], document_type="Draft")

    assert found["report.md"].document_sha256 == "bbb"


@pytest.mark.asyncio
async def test_filesystem_find_documents_by_name_tiebreaks_by_sha256(tmp_path: Path) -> None:
    database = FilesystemDatabase(tmp_path)
    doc_low = _make_document(document_sha256="aaa-low", name="report.md")
    doc_high = _make_document(document_sha256="zzz-high", name="report.md")
    for doc in (doc_low, doc_high):
        await database.save_document(doc)

    found = await database.find_documents_by_name(["report.md"])

    assert found["report.md"].document_sha256 == "zzz-high"


@pytest.mark.asyncio
async def test_filesystem_find_documents_by_name_empty_returns_empty(tmp_path: Path) -> None:
    database = FilesystemDatabase(tmp_path)
    await database.save_document(_make_document(name="report.md"))

    found = await database.find_documents_by_name([])

    assert found == {}


@pytest.mark.asyncio
async def test_filesystem_get_all_document_shas_for_tree(tmp_path: Path) -> None:
    database, _ = await _seed_database(tmp_path)
    deployment = await database.get_deployment_by_run_id(next(iter(database._spans.values())).run_id)
    assert deployment is not None

    shas = await database.get_all_document_shas_for_tree(deployment.root_deployment_id)

    assert shas == {"doc-1"}
