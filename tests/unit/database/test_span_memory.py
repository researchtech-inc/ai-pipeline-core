"""Tests for the span-oriented in-memory database backend."""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from ai_pipeline_core.database import DocumentRecord, LogRecord, SpanKind, SpanRecord, SpanStatus
from ai_pipeline_core.database._documents import load_documents_from_database
from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.database._types import _BlobRecord
from ai_pipeline_core.documents import Attachment, Document


class MemoryLoadDoc(Document):
    """Document type used for load_documents_from_database coverage."""


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


async def _seed_database() -> tuple[_MemoryDatabase, UUID, UUID]:
    database = _MemoryDatabase()
    root_deployment_id = uuid4()
    child_deployment_id = uuid4()
    base = datetime(2026, 3, 11, 12, 0, tzinfo=UTC)

    root = _make_span(
        span_id=root_deployment_id,
        deployment_id=root_deployment_id,
        root_deployment_id=root_deployment_id,
        kind=SpanKind.DEPLOYMENT,
        name="RootDeployment",
        started_at=base,
    )
    task = _make_span(
        parent_span_id=root.span_id,
        deployment_id=root_deployment_id,
        root_deployment_id=root_deployment_id,
        kind=SpanKind.TASK,
        name="RootTask",
        sequence_no=1,
        started_at=base + timedelta(seconds=10),
        output_document_shas=("doc-root",),
    )
    llm_round = _make_span(
        parent_span_id=task.span_id,
        deployment_id=root_deployment_id,
        root_deployment_id=root_deployment_id,
        kind=SpanKind.LLM_ROUND,
        name="Round1",
        sequence_no=1,
        started_at=base + timedelta(seconds=11),
        cost_usd=1.5,
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
    child = _make_span(
        span_id=child_deployment_id,
        parent_span_id=task.span_id,
        deployment_id=child_deployment_id,
        root_deployment_id=root_deployment_id,
        kind=SpanKind.DEPLOYMENT,
        name="ChildDeployment",
        sequence_no=2,
        started_at=base + timedelta(minutes=1),
    )
    child_task = _make_span(
        parent_span_id=child.span_id,
        deployment_id=child_deployment_id,
        root_deployment_id=root_deployment_id,
        kind=SpanKind.TASK,
        name="ChildTask",
        sequence_no=1,
        started_at=base + timedelta(minutes=1, seconds=5),
        input_document_shas=("doc-root",),
    )

    for span in (root, task, llm_round, child, child_task):
        await database.insert_span(span)

    document = _make_document(
        document_sha256="doc-root",
        content_sha256="blob-root",
        document_type="RootDoc",
        name="root.md",
        attachment_names=("preview.png",),
        attachment_descriptions=("Preview image",),
        attachment_content_sha256s=("blob-preview",),
        attachment_mime_types=("image/png",),
        attachment_size_bytes=(7,),
    )
    await database.save_document(document)
    await database.save_blob(_make_blob(content_sha256="blob-root", content=b"root"))
    await database.save_blob(_make_blob(content_sha256="blob-preview", content=b"pngdata"))
    await database.save_logs_batch([
        _make_log(
            deployment_id=root_deployment_id,
            span_id=task.span_id,
            timestamp=base + timedelta(seconds=20),
            sequence_no=1,
            message="root log",
        )
    ])

    return database, root_deployment_id, child_deployment_id


@pytest.mark.asyncio
async def test_memory_database_reads_spans_documents_blobs_and_logs() -> None:
    database, root_deployment_id, _ = await _seed_database()

    tree = await database.get_deployment_tree(root_deployment_id)
    document = await database.get_document("doc-root")
    hydrated = await database.get_document_with_content("doc-root")
    totals = await database.get_deployment_cost_totals(root_deployment_id)
    logs = await database.get_deployment_logs(root_deployment_id)

    assert [span.kind for span in tree] == [
        SpanKind.DEPLOYMENT,
        SpanKind.TASK,
        SpanKind.LLM_ROUND,
        SpanKind.DEPLOYMENT,
        SpanKind.TASK,
    ]
    assert document is not None
    assert document.mime_type == "text/markdown"
    assert hydrated is not None
    assert hydrated.content == b"root"
    assert hydrated.attachment_contents == {"blob-preview": b"pngdata"}
    assert totals.cost_usd == 1.5
    assert totals.tokens_input == 100
    assert totals.tokens_output == 25
    assert len(logs) == 1


@pytest.mark.asyncio
async def test_get_all_document_shas_for_tree_collects_inputs_and_outputs() -> None:
    database, root_deployment_id, _ = await _seed_database()

    shas = await database.get_all_document_shas_for_tree(root_deployment_id)

    assert shas == {"doc-root"}


@pytest.mark.asyncio
async def test_tree_topology_and_activity_helpers_match_memory_backend_behavior() -> None:
    database, root_deployment_id, _ = await _seed_database()

    topology = await database.get_deployment_tree_topology(root_deployment_id)
    latest_activity = await database.get_deployment_latest_activity(root_deployment_id)
    reaper_activity = await database.latest_span_activity_for_deployment(root_deployment_id)

    assert topology == await database.get_deployment_tree(root_deployment_id)
    assert latest_activity == reaper_activity
    assert latest_activity == max(span.ended_at or span.started_at for span in topology)


@pytest.mark.asyncio
async def test_list_running_deployment_roots_returns_oldest_first() -> None:
    database = _MemoryDatabase()
    base = datetime(2026, 3, 11, 12, 0, tzinfo=UTC)
    newest = _make_span(
        span_id=uuid4(),
        deployment_id=uuid4(),
        root_deployment_id=uuid4(),
        kind=SpanKind.DEPLOYMENT,
        status=SpanStatus.RUNNING,
        started_at=base + timedelta(hours=2),
    )
    newest = _make_span(
        span_id=newest.span_id,
        deployment_id=newest.span_id,
        root_deployment_id=newest.span_id,
        kind=SpanKind.DEPLOYMENT,
        status=SpanStatus.RUNNING,
        started_at=base + timedelta(hours=2),
    )
    oldest = _make_span(
        span_id=uuid4(),
        deployment_id=uuid4(),
        root_deployment_id=uuid4(),
        kind=SpanKind.DEPLOYMENT,
        status=SpanStatus.RUNNING,
        started_at=base,
    )
    oldest = _make_span(
        span_id=oldest.span_id,
        deployment_id=oldest.span_id,
        root_deployment_id=oldest.span_id,
        kind=SpanKind.DEPLOYMENT,
        status=SpanStatus.RUNNING,
        started_at=base,
    )
    for span in (newest, oldest):
        await database.insert_span(span)

    roots = await database.list_running_deployment_roots(limit=2)

    assert [span.span_id for span in roots] == [oldest.span_id, newest.span_id]


@pytest.mark.asyncio
async def test_update_document_summary_changes_only_summary() -> None:
    database = _MemoryDatabase()
    original = _make_document(document_sha256="doc-1", summary="old")
    await database.save_document(original)

    await database.update_document_summary("doc-1", "updated")
    updated = await database.get_document("doc-1")

    assert updated is not None
    assert updated.summary == "updated"
    assert updated.name == original.name


@pytest.mark.asyncio
async def test_save_document_insert_once() -> None:
    database = _MemoryDatabase()
    first = _make_document(document_sha256="doc-1", summary="first")
    second = _make_document(document_sha256="doc-1", summary="second")

    await database.save_document(first)
    await database.save_document(second)
    stored = await database.get_document("doc-1")
    assert stored is not None
    assert stored.summary == "first"


@pytest.mark.asyncio
async def test_get_document_with_content_raises_for_missing_attachment_blob() -> None:
    database = _MemoryDatabase()
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
async def test_load_documents_from_database_reconstructs_attachments() -> None:
    database = _MemoryDatabase()
    document = MemoryLoadDoc(
        name="report.md",
        content=b"# Report",
        description="Stored report",
        attachments=(Attachment(name="details.txt", content=b"details", description="More detail"),),
    )
    record = _make_document(
        document_sha256=document.sha256,
        content_sha256=document.content_sha256,
        document_type=type(document).__name__,
        name=document.name,
        description=document.description or "",
        mime_type=document.mime_type,
        size_bytes=document.size,
        attachment_names=tuple(attachment.name for attachment in document.attachments),
        attachment_descriptions=tuple((attachment.description or "") for attachment in document.attachments),
        attachment_content_sha256s=("blob-details",),
        attachment_mime_types=tuple(attachment.mime_type for attachment in document.attachments),
        attachment_size_bytes=tuple(attachment.size for attachment in document.attachments),
    )
    await database.save_document(record)
    await database.save_blob(_make_blob(content_sha256=document.content_sha256, content=document.content))
    await database.save_blob(_make_blob(content_sha256="blob-details", content=b"details"))

    loaded = await load_documents_from_database(database, {document.sha256})

    assert len(loaded) == 1
    assert isinstance(loaded[0], MemoryLoadDoc)
    assert loaded[0].description == "Stored report"
    assert loaded[0].attachments[0].name == "details.txt"


@pytest.mark.asyncio
async def test_get_cached_completion_filters_by_max_age() -> None:
    database = _MemoryDatabase()
    now = datetime.now(UTC)
    recent = _make_span(
        span_id=uuid4(),
        cache_key="cache-key",
        status=SpanStatus.COMPLETED,
        started_at=now - timedelta(minutes=2),
        ended_at=now - timedelta(minutes=1),
    )
    stale = _make_span(
        span_id=uuid4(),
        cache_key="cache-key",
        status=SpanStatus.COMPLETED,
        started_at=now - timedelta(days=2),
        ended_at=now - timedelta(days=2) + timedelta(seconds=1),
    )
    await database.insert_span(recent)
    await database.insert_span(stale)

    cached = await database.get_cached_completion("cache-key", max_age=timedelta(hours=1))

    assert cached == recent


@pytest.mark.asyncio
async def test_find_documents_by_name_returns_matching_records() -> None:
    database = _MemoryDatabase()
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
async def test_find_documents_by_name_filters_by_document_type() -> None:
    database = _MemoryDatabase()
    doc_a = _make_document(document_sha256="aaa", name="report.md", document_type="Report")
    doc_b = _make_document(document_sha256="bbb", name="report.md", document_type="Draft")
    for doc in (doc_a, doc_b):
        await database.save_document(doc)

    found = await database.find_documents_by_name(["report.md"], document_type="Draft")

    assert found["report.md"].document_sha256 == "bbb"


@pytest.mark.asyncio
async def test_find_documents_by_name_tiebreaks_by_highest_sha256() -> None:
    database = _MemoryDatabase()
    doc_low = _make_document(document_sha256="aaa-low", name="report.md")
    doc_high = _make_document(document_sha256="zzz-high", name="report.md")
    for doc in (doc_low, doc_high):
        await database.save_document(doc)

    found = await database.find_documents_by_name(["report.md"])

    assert found["report.md"].document_sha256 == "zzz-high"


@pytest.mark.asyncio
async def test_find_documents_by_name_empty_names_returns_empty() -> None:
    database = _MemoryDatabase()
    await database.save_document(_make_document(name="report.md"))

    found = await database.find_documents_by_name([])

    assert found == {}


@pytest.mark.asyncio
async def test_find_documents_by_name_no_matches_returns_empty() -> None:
    database = _MemoryDatabase()
    await database.save_document(_make_document(name="report.md"))

    found = await database.find_documents_by_name(["nonexistent.md"])

    assert found == {}


@pytest.mark.asyncio
async def test_get_span_logs_filters_level_and_category() -> None:
    database = _MemoryDatabase()
    span_id = uuid4()
    deployment_id = uuid4()
    await database.save_logs_batch([
        _make_log(span_id=span_id, deployment_id=deployment_id, level="INFO", category="framework", message="a"),
        _make_log(span_id=span_id, deployment_id=deployment_id, level="ERROR", category="user", message="b", sequence_no=1),
    ])

    logs = await database.get_span_logs(span_id, level="ERROR")

    assert [log.message for log in logs] == ["b"]
