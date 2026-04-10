"""Integration tests for ClickHouseDatabase using testcontainers.

These tests spin up a real ClickHouse instance to validate the full
DatabaseWriter/DatabaseReader protocol — spans, documents, blobs, logs,
and query methods — against the actual database engine.
"""

import json
import logging
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from uuid import uuid4

import pytest
from testcontainers.clickhouse import ClickHouseContainer

from ai_pipeline_core.database._types import (
    _BlobRecord,
    CostTotals,
    DocumentRecord,
    LogRecord,
    SpanKind,
    SpanRecord,
    SpanStatus,
)
from ai_pipeline_core.database.clickhouse._backend import ClickHouseDatabase
from ai_pipeline_core.database.clickhouse._connection import reset_schema_check
from ai_pipeline_core.deployment._types import _NoopPublisher
from ai_pipeline_core.logger._buffer import ExecutionLogBuffer
from ai_pipeline_core.logger._handler import ExecutionLogHandler
from ai_pipeline_core.pipeline._execution_context import ExecutionContext, set_execution_context
from ai_pipeline_core.pipeline.limits import _SharedStatus
from ai_pipeline_core.settings import Settings

HTTP_PORT = 8123


@pytest.fixture(scope="module")
def clickhouse_container():
    """Start a ClickHouse container once per module, exposing the HTTP port."""
    container = ClickHouseContainer(port=HTTP_PORT)
    container.with_exposed_ports(HTTP_PORT)
    with container:
        yield container


@pytest.fixture(scope="module")
def clickhouse_settings(clickhouse_container: ClickHouseContainer) -> Settings:
    """Build Settings pointing at the testcontainer ClickHouse instance."""
    host = clickhouse_container.get_container_host_ip()
    port = int(clickhouse_container.get_exposed_port(HTTP_PORT))
    return Settings(
        clickhouse_host=host,
        clickhouse_port=port,
        clickhouse_database=clickhouse_container.dbname,
        clickhouse_user=clickhouse_container.username,
        clickhouse_password=clickhouse_container.password,
        clickhouse_secure=False,
        clickhouse_connect_timeout=30,
        clickhouse_send_receive_timeout=60,
    )


@pytest.fixture(autouse=True)
def _reset_schema():
    """Reset schema check flag before each test so DDL runs on first connect."""
    reset_schema_check()
    yield
    reset_schema_check()


@pytest.fixture
def database(clickhouse_settings: Settings) -> ClickHouseDatabase:
    return ClickHouseDatabase(settings=clickhouse_settings)


# ---------------------------------------------------------------------------
# Test data factories
# ---------------------------------------------------------------------------

_BASE_TIME = datetime(2026, 3, 11, 12, 0, tzinfo=UTC)


def _make_span(**kwargs: object) -> SpanRecord:
    deployment_id = kwargs.pop("deployment_id", uuid4())
    root_deployment_id = kwargs.pop("root_deployment_id", deployment_id)
    started_at: datetime = kwargs.pop("started_at", _BASE_TIME)
    defaults: dict[str, object] = {
        "span_id": kwargs.pop("span_id", uuid4()),
        "parent_span_id": kwargs.pop("parent_span_id", None),
        "deployment_id": deployment_id,
        "root_deployment_id": root_deployment_id,
        "run_id": kwargs.pop("run_id", f"run-{deployment_id}"),
        "deployment_name": "integration-test",
        "kind": SpanKind.TASK,
        "name": "TestTask",
        "status": SpanStatus.COMPLETED,
        "sequence_no": 0,
        "started_at": started_at,
        "ended_at": started_at + timedelta(seconds=1),
        "version": 1,
    }
    defaults.update(kwargs)
    return SpanRecord(**defaults)


def _make_document(**kwargs: object) -> DocumentRecord:
    defaults: dict[str, object] = {
        "document_sha256": f"doc-{uuid4().hex[:16]}",
        "content_sha256": f"blob-{uuid4().hex[:16]}",
        "document_type": "TestDoc",
        "name": "test.md",
        "description": "test document",
        "mime_type": "text/markdown",
        "size_bytes": 10,
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
        "content_sha256": f"blob-{uuid4().hex[:16]}",
        "content": b"blob-content",
    }
    defaults.update(kwargs)
    return _BlobRecord(**defaults)


def _make_log(**kwargs: object) -> LogRecord:
    defaults: dict[str, object] = {
        "deployment_id": uuid4(),
        "span_id": uuid4(),
        "timestamp": _BASE_TIME,
        "sequence_no": 0,
        "level": "INFO",
        "category": "framework",
        "event_type": "",
        "logger_name": "test",
        "message": "test log",
        "fields_json": "{}",
        "exception_text": "",
    }
    defaults.update(kwargs)
    return LogRecord(**defaults)


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schema_auto_creates_tables(database: ClickHouseDatabase) -> None:
    """First connection creates all tables and stamps schema_meta."""
    # Trigger schema init by calling any query — succeeds without raising
    result = await database.list_deployments(1)
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Span CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_and_get_span(database: ClickHouseDatabase) -> None:
    span = _make_span()
    await database.insert_span(span)

    loaded = await database.get_span(span.span_id)

    assert loaded is not None
    assert loaded.span_id == span.span_id
    assert loaded.kind == SpanKind.TASK
    assert loaded.name == "TestTask"
    assert loaded.status == SpanStatus.COMPLETED


@pytest.mark.asyncio
async def test_span_version_upsert(database: ClickHouseDatabase) -> None:
    """ReplacingMergeTree replaces rows with higher version on FINAL queries."""
    span_id = uuid4()
    deployment_id = uuid4()
    v1 = _make_span(
        span_id=span_id,
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        status=SpanStatus.RUNNING,
        version=1,
    )
    v2 = _make_span(
        span_id=span_id,
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        status=SpanStatus.COMPLETED,
        version=2,
        ended_at=_BASE_TIME + timedelta(seconds=5),
    )
    await database.insert_span(v1)
    await database.insert_span(v2)

    loaded = await database.get_span(span_id)

    assert loaded is not None
    assert loaded.status == SpanStatus.COMPLETED
    assert loaded.version == 2


# ---------------------------------------------------------------------------
# Deployment tree & hierarchy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deployment_tree_hierarchy(database: ClickHouseDatabase) -> None:
    root_id = uuid4()
    flow_id = uuid4()
    task_id = uuid4()
    conv_id = uuid4()
    run_id = f"run-{root_id}"

    root = _make_span(
        span_id=root_id,
        deployment_id=root_id,
        root_deployment_id=root_id,
        kind=SpanKind.DEPLOYMENT,
        name="Deploy",
        run_id=run_id,
        started_at=_BASE_TIME,
    )
    flow = _make_span(
        span_id=flow_id,
        parent_span_id=root_id,
        deployment_id=root_id,
        root_deployment_id=root_id,
        kind=SpanKind.FLOW,
        name="Flow1",
        sequence_no=1,
        run_id=run_id,
        started_at=_BASE_TIME + timedelta(seconds=1),
    )
    task = _make_span(
        span_id=task_id,
        parent_span_id=flow_id,
        deployment_id=root_id,
        root_deployment_id=root_id,
        kind=SpanKind.TASK,
        name="Task1",
        sequence_no=1,
        run_id=run_id,
        started_at=_BASE_TIME + timedelta(seconds=2),
    )
    conv = _make_span(
        span_id=conv_id,
        parent_span_id=task_id,
        deployment_id=root_id,
        root_deployment_id=root_id,
        kind=SpanKind.CONVERSATION,
        name="Conv1",
        sequence_no=1,
        run_id=run_id,
        started_at=_BASE_TIME + timedelta(seconds=3),
    )
    for span in (root, flow, task, conv):
        await database.insert_span(span)

    tree = await database.get_deployment_tree(root_id)
    kinds = [s.kind for s in tree]

    assert SpanKind.DEPLOYMENT in kinds
    assert SpanKind.FLOW in kinds
    assert SpanKind.TASK in kinds
    assert SpanKind.CONVERSATION in kinds
    assert len(tree) == 4


@pytest.mark.asyncio
async def test_deployment_tree_topology_blanks_payload_json_and_preserves_order(database: ClickHouseDatabase) -> None:
    root_id = uuid4()
    run_id = f"topology-{uuid4().hex[:8]}"
    root = _make_span(
        span_id=root_id,
        deployment_id=root_id,
        root_deployment_id=root_id,
        kind=SpanKind.DEPLOYMENT,
        name="Deploy",
        run_id=run_id,
        started_at=_BASE_TIME + timedelta(seconds=5),
        input_json=json.dumps({"input": True}),
        output_json=json.dumps({"output": True}),
        meta_json=json.dumps({"meta": True}),
        metrics_json=json.dumps({"tokens_input": 1}),
        receiver_json=json.dumps({"receiver": True}),
        error_json=json.dumps({"error": True}),
    )
    child = _make_span(
        parent_span_id=root_id,
        deployment_id=root_id,
        root_deployment_id=root_id,
        kind=SpanKind.TASK,
        name="Child",
        sequence_no=1,
        run_id=run_id,
        started_at=_BASE_TIME,
        input_json=json.dumps({"child": True}),
    )
    await database.insert_span(root)
    await database.insert_span(child)

    topology = await database.get_deployment_tree_topology(root_id)

    assert [span.name for span in topology] == ["Child", "Deploy"]
    assert topology[0].input_json == ""
    assert topology[0].output_json == ""
    assert topology[0].meta_json == ""
    assert topology[0].metrics_json == ""
    assert topology[0].receiver_json == ""
    assert topology[0].error_json == ""
    assert topology[0].error_message == ""


@pytest.mark.asyncio
async def test_deployment_activity_helpers_and_running_root_order(database: ClickHouseDatabase) -> None:
    now = _BASE_TIME
    oldest_root_id = uuid4()
    newest_root_id = uuid4()

    oldest_root = _make_span(
        span_id=oldest_root_id,
        deployment_id=oldest_root_id,
        root_deployment_id=oldest_root_id,
        kind=SpanKind.DEPLOYMENT,
        name="Oldest",
        status=SpanStatus.RUNNING,
        started_at=now,
        ended_at=None,
    )
    newest_root = _make_span(
        span_id=newest_root_id,
        deployment_id=newest_root_id,
        root_deployment_id=newest_root_id,
        kind=SpanKind.DEPLOYMENT,
        name="Newest",
        status=SpanStatus.RUNNING,
        started_at=now + timedelta(hours=1),
        ended_at=None,
    )
    latest_child = _make_span(
        parent_span_id=oldest_root_id,
        deployment_id=oldest_root_id,
        root_deployment_id=oldest_root_id,
        kind=SpanKind.TASK,
        name="LatestChild",
        status=SpanStatus.RUNNING,
        started_at=now + timedelta(hours=2),
        ended_at=now + timedelta(hours=3),
    )
    for span in (oldest_root, newest_root, latest_child):
        await database.insert_span(span)

    running_roots = await database.list_running_deployment_roots(limit=2)
    latest_activity = await database.latest_span_activity_for_deployment(oldest_root_id)
    live_activity = await database.get_deployment_latest_activity(oldest_root_id)

    assert [span.span_id for span in running_roots] == [oldest_root_id, newest_root_id]
    assert latest_activity == now + timedelta(hours=3)
    assert live_activity == latest_activity


@pytest.mark.asyncio
async def test_get_child_spans(database: ClickHouseDatabase) -> None:
    parent_id = uuid4()
    deployment_id = uuid4()
    run_id = f"run-{deployment_id}"
    parent = _make_span(
        span_id=parent_id,
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        kind=SpanKind.FLOW,
        name="Parent",
        run_id=run_id,
    )
    child_a = _make_span(
        span_id=uuid4(),
        parent_span_id=parent_id,
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        kind=SpanKind.TASK,
        name="ChildA",
        sequence_no=0,
        run_id=run_id,
        started_at=_BASE_TIME + timedelta(seconds=1),
    )
    child_b = _make_span(
        span_id=uuid4(),
        parent_span_id=parent_id,
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        kind=SpanKind.TASK,
        name="ChildB",
        sequence_no=1,
        run_id=run_id,
        started_at=_BASE_TIME + timedelta(seconds=2),
    )
    for span in (parent, child_a, child_b):
        await database.insert_span(span)

    children = await database.get_child_spans(parent_id)

    assert len(children) == 2
    assert children[0].name == "ChildA"
    assert children[1].name == "ChildB"


# ---------------------------------------------------------------------------
# Deployment queries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_deployment_by_run_id(database: ClickHouseDatabase) -> None:
    deployment_id = uuid4()
    run_id = f"run-unique-{uuid4().hex[:8]}"
    deploy = _make_span(
        span_id=deployment_id,
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        kind=SpanKind.DEPLOYMENT,
        name="DeployByRunId",
        run_id=run_id,
    )
    await database.insert_span(deploy)

    loaded = await database.get_deployment_by_run_id(run_id)

    assert loaded is not None
    assert loaded.span_id == deployment_id


@pytest.mark.asyncio
async def test_list_deployments_with_status_filter(database: ClickHouseDatabase) -> None:
    dep_completed = uuid4()
    dep_failed = uuid4()
    run_prefix = f"list-{uuid4().hex[:8]}"
    await database.insert_span(
        _make_span(
            span_id=dep_completed,
            deployment_id=dep_completed,
            root_deployment_id=dep_completed,
            kind=SpanKind.DEPLOYMENT,
            name="Completed",
            status=SpanStatus.COMPLETED,
            run_id=f"{run_prefix}-ok",
        )
    )
    await database.insert_span(
        _make_span(
            span_id=dep_failed,
            deployment_id=dep_failed,
            root_deployment_id=dep_failed,
            kind=SpanKind.DEPLOYMENT,
            name="Failed",
            status=SpanStatus.FAILED,
            run_id=f"{run_prefix}-fail",
        )
    )

    completed = await database.list_deployments(100, status=SpanStatus.COMPLETED)
    completed_ids = {s.span_id for s in completed}

    assert dep_completed in completed_ids
    assert dep_failed not in completed_ids


@pytest.mark.asyncio
async def test_get_deployment_span_count(database: ClickHouseDatabase) -> None:
    root_id = uuid4()
    run_id = f"count-{uuid4().hex[:8]}"
    await database.insert_span(
        _make_span(
            span_id=root_id,
            deployment_id=root_id,
            root_deployment_id=root_id,
            kind=SpanKind.DEPLOYMENT,
            name="Root",
            run_id=run_id,
        )
    )
    await database.insert_span(
        _make_span(
            parent_span_id=root_id,
            deployment_id=root_id,
            root_deployment_id=root_id,
            kind=SpanKind.TASK,
            name="T1",
            sequence_no=1,
            run_id=run_id,
            started_at=_BASE_TIME + timedelta(seconds=1),
        )
    )
    await database.insert_span(
        _make_span(
            parent_span_id=root_id,
            deployment_id=root_id,
            root_deployment_id=root_id,
            kind=SpanKind.TASK,
            name="T2",
            sequence_no=2,
            run_id=run_id,
            started_at=_BASE_TIME + timedelta(seconds=2),
        )
    )

    total = await database.get_deployment_span_count(root_id)
    tasks_only = await database.get_deployment_span_count(root_id, kinds=[SpanKind.TASK])

    assert total == 3
    assert tasks_only == 2


# ---------------------------------------------------------------------------
# Document CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_and_get_document(database: ClickHouseDatabase) -> None:
    doc = _make_document(derived_from=("source-sha-1",))
    await database.save_document(doc)

    loaded = await database.get_document(doc.document_sha256)

    assert loaded is not None
    assert loaded.document_sha256 == doc.document_sha256
    assert loaded.name == "test.md"
    assert loaded.derived_from == ("source-sha-1",)


@pytest.mark.asyncio
async def test_save_document_batch_and_get_batch(database: ClickHouseDatabase) -> None:
    docs = [_make_document() for _ in range(3)]
    await database.save_document_batch(docs)

    shas = [d.document_sha256 for d in docs]
    batch = await database.get_documents_batch(shas)

    assert len(batch) == 3
    for sha in shas:
        assert sha in batch


@pytest.mark.asyncio
async def test_document_with_attachments(database: ClickHouseDatabase) -> None:
    doc = _make_document(
        attachment_names=("screenshot.png",),
        attachment_descriptions=("Screen capture",),
        attachment_content_sha256s=("att-blob-sha",),
        attachment_mime_types=("image/png",),
        attachment_size_bytes=(1024,),
    )
    await database.save_document(doc)

    loaded = await database.get_document(doc.document_sha256)

    assert loaded is not None
    assert loaded.attachment_names == ("screenshot.png",)
    assert loaded.attachment_content_sha256s == ("att-blob-sha",)
    assert loaded.attachment_size_bytes == (1024,)


@pytest.mark.asyncio
async def test_update_document_summary(database: ClickHouseDatabase) -> None:
    doc = _make_document(summary="original")
    await database.save_document(doc)

    await database.update_document_summary(doc.document_sha256, "updated summary")

    loaded = await database.get_document(doc.document_sha256)
    assert loaded is not None
    assert loaded.summary == "updated summary"


# ---------------------------------------------------------------------------
# Blob CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_and_get_blob(database: ClickHouseDatabase) -> None:
    blob = _make_blob(content=b"hello world")
    await database.save_blob(blob)

    loaded = await database.get_blob(blob.content_sha256)

    assert loaded is not None
    assert loaded.content == b"hello world"


@pytest.mark.asyncio
async def test_save_blob_batch_and_get_batch(database: ClickHouseDatabase) -> None:
    blobs = [_make_blob(content=f"data-{i}".encode()) for i in range(3)]
    await database.save_blob_batch(blobs)

    shas = [b.content_sha256 for b in blobs]
    batch = await database.get_blobs_batch(shas)

    assert len(batch) == 3


# ---------------------------------------------------------------------------
# Hydrated document (document + blob content)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_document_with_content(database: ClickHouseDatabase) -> None:
    content_sha = f"blob-hydrate-{uuid4().hex[:8]}"
    att_sha = f"blob-att-{uuid4().hex[:8]}"
    doc = _make_document(
        content_sha256=content_sha,
        attachment_names=("extra.txt",),
        attachment_descriptions=("Extra data",),
        attachment_content_sha256s=(att_sha,),
        attachment_mime_types=("text/plain",),
        attachment_size_bytes=(5,),
    )
    await database.save_document(doc)
    await database.save_blob(_make_blob(content_sha256=content_sha, content=b"primary content"))
    await database.save_blob(_make_blob(content_sha256=att_sha, content=b"extra"))

    hydrated = await database.get_document_with_content(doc.document_sha256)

    assert hydrated is not None
    assert hydrated.content == b"primary content"
    assert hydrated.attachment_contents[att_sha] == b"extra"


@pytest.mark.asyncio
async def test_get_document_with_content_returns_none_for_missing(database: ClickHouseDatabase) -> None:
    result = await database.get_document_with_content("nonexistent-sha")
    assert result is None


# ---------------------------------------------------------------------------
# Log CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_and_get_logs(database: ClickHouseDatabase) -> None:
    deployment_id = uuid4()
    span_id = uuid4()
    logs = [
        _make_log(deployment_id=deployment_id, span_id=span_id, message="msg-1", sequence_no=0),
        _make_log(deployment_id=deployment_id, span_id=span_id, message="msg-2", sequence_no=1, level="ERROR"),
    ]
    await database.save_logs_batch(logs)

    span_logs = await database.get_span_logs(span_id)
    assert len(span_logs) == 2

    error_logs = await database.get_span_logs(span_id, level="ERROR")
    assert len(error_logs) == 1
    assert error_logs[0].message == "msg-2"


@pytest.mark.asyncio
async def test_get_deployment_logs(database: ClickHouseDatabase) -> None:
    deployment_id = uuid4()
    span_a = uuid4()
    span_b = uuid4()
    await database.save_logs_batch([
        _make_log(deployment_id=deployment_id, span_id=span_a, message="log-a"),
        _make_log(deployment_id=deployment_id, span_id=span_b, message="log-b", sequence_no=1),
    ])

    logs = await database.get_deployment_logs(deployment_id)

    assert len(logs) == 2
    messages = {log.message for log in logs}
    assert messages == {"log-a", "log-b"}


@pytest.mark.asyncio
async def test_get_deployment_logs_batch(database: ClickHouseDatabase) -> None:
    dep_a = uuid4()
    dep_b = uuid4()
    await database.save_logs_batch([
        _make_log(deployment_id=dep_a, message="a"),
        _make_log(deployment_id=dep_b, message="b"),
    ])

    logs = await database.get_deployment_logs_batch([dep_a, dep_b])

    messages = {log.message for log in logs}
    assert "a" in messages
    assert "b" in messages


@pytest.mark.asyncio
async def test_execution_log_handler_persists_service_in_fields_json(database: ClickHouseDatabase) -> None:
    deployment_id = uuid4()
    span_id = uuid4()
    log_buffer = ExecutionLogBuffer(request_flush=lambda: None)
    ctx = ExecutionContext(
        run_id="service-run",
        execution_id=None,
        publisher=_NoopPublisher(),
        limits=MappingProxyType({}),
        limits_status=_SharedStatus(),
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        service_name="research",
        span_id=span_id,
        current_span_id=span_id,
        log_buffer=log_buffer,
    )
    record = logging.LogRecord("my.app", logging.INFO, __file__, 1, "service log", (), None)
    record.fields_json = {"step": "emit"}

    with set_execution_context(ctx):
        ExecutionLogHandler().emit(record)

    await database.save_logs_batch(log_buffer.drain())
    loaded_logs = await database.get_span_logs(span_id)

    assert len(loaded_logs) == 1
    assert json.loads(loaded_logs[0].fields_json) == {"service": "research", "step": "emit"}


# ---------------------------------------------------------------------------
# Cost aggregation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_deployment_cost_totals(database: ClickHouseDatabase) -> None:
    root_id = uuid4()
    run_id = f"cost-{uuid4().hex[:8]}"
    task_id = uuid4()
    await database.insert_span(
        _make_span(
            span_id=root_id,
            deployment_id=root_id,
            root_deployment_id=root_id,
            kind=SpanKind.DEPLOYMENT,
            name="Root",
            run_id=run_id,
        )
    )
    await database.insert_span(
        _make_span(
            span_id=task_id,
            parent_span_id=root_id,
            deployment_id=root_id,
            root_deployment_id=root_id,
            kind=SpanKind.TASK,
            name="T",
            run_id=run_id,
            started_at=_BASE_TIME + timedelta(seconds=1),
        )
    )
    await database.insert_span(
        _make_span(
            parent_span_id=task_id,
            deployment_id=root_id,
            root_deployment_id=root_id,
            kind=SpanKind.LLM_ROUND,
            name="R1",
            run_id=run_id,
            cost_usd=0.5,
            metrics_json=json.dumps({
                "tokens_input": 100,
                "tokens_output": 50,
                "tokens_cache_read": 20,
                "tokens_reasoning": 10,
            }),
            started_at=_BASE_TIME + timedelta(seconds=2),
        )
    )
    await database.insert_span(
        _make_span(
            parent_span_id=task_id,
            deployment_id=root_id,
            root_deployment_id=root_id,
            kind=SpanKind.LLM_ROUND,
            name="R2",
            sequence_no=1,
            run_id=run_id,
            cost_usd=0.3,
            metrics_json=json.dumps({
                "tokens_input": 200,
                "tokens_output": 75,
                "tokens_cache_read": 50,
                "tokens_reasoning": 0,
            }),
            started_at=_BASE_TIME + timedelta(seconds=3),
        )
    )

    totals = await database.get_deployment_cost_totals(root_id)

    assert totals.cost_usd == pytest.approx(0.8)
    assert totals.tokens_input == 300
    assert totals.tokens_output == 125
    assert totals.tokens_cache_read == 70
    assert totals.tokens_reasoning == 10


# ---------------------------------------------------------------------------
# Document reference queries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_spans_referencing_document(database: ClickHouseDatabase) -> None:
    root_id = uuid4()
    run_id = f"ref-{uuid4().hex[:8]}"
    doc_sha = f"doc-ref-{uuid4().hex[:8]}"
    span_with_ref = _make_span(
        deployment_id=root_id,
        root_deployment_id=root_id,
        kind=SpanKind.TASK,
        name="WithRef",
        run_id=run_id,
        input_document_shas=(doc_sha,),
    )
    span_without = _make_span(
        deployment_id=root_id,
        root_deployment_id=root_id,
        kind=SpanKind.TASK,
        name="NoRef",
        sequence_no=1,
        run_id=run_id,
        started_at=_BASE_TIME + timedelta(seconds=1),
    )
    await database.insert_span(span_with_ref)
    await database.insert_span(span_without)

    refs = await database.get_spans_referencing_document(doc_sha)

    assert len(refs) == 1
    assert refs[0].span_id == span_with_ref.span_id


@pytest.mark.asyncio
async def test_get_all_document_shas_for_tree(database: ClickHouseDatabase) -> None:
    root_id = uuid4()
    run_id = f"shas-{uuid4().hex[:8]}"
    await database.insert_span(
        _make_span(
            span_id=root_id,
            deployment_id=root_id,
            root_deployment_id=root_id,
            kind=SpanKind.DEPLOYMENT,
            name="Root",
            run_id=run_id,
            input_document_shas=("sha-a",),
        )
    )
    await database.insert_span(
        _make_span(
            parent_span_id=root_id,
            deployment_id=root_id,
            root_deployment_id=root_id,
            kind=SpanKind.TASK,
            name="T",
            run_id=run_id,
            output_document_shas=("sha-b", "sha-c"),
            started_at=_BASE_TIME + timedelta(seconds=1),
        )
    )

    shas = await database.get_all_document_shas_for_tree(root_id)

    assert shas == {"sha-a", "sha-b", "sha-c"}


# ---------------------------------------------------------------------------
# Cache lookup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_cached_completion(database: ClickHouseDatabase) -> None:
    cache_key = f"cache-{uuid4().hex[:8]}"
    now = datetime.now(UTC)
    span = _make_span(
        kind=SpanKind.CONVERSATION,
        name="CachedConv",
        cache_key=cache_key,
        status=SpanStatus.COMPLETED,
        started_at=now - timedelta(minutes=2),
        ended_at=now - timedelta(minutes=1),
    )
    await database.insert_span(span)

    cached = await database.get_cached_completion(cache_key, max_age=timedelta(hours=1))

    assert cached is not None
    assert cached.span_id == span.span_id


@pytest.mark.asyncio
async def test_get_cached_completion_respects_max_age(database: ClickHouseDatabase) -> None:
    cache_key = f"stale-{uuid4().hex[:8]}"
    old_time = datetime(2020, 1, 1, tzinfo=UTC)
    span = _make_span(
        kind=SpanKind.CONVERSATION,
        name="StaleConv",
        cache_key=cache_key,
        status=SpanStatus.COMPLETED,
        started_at=old_time,
        ended_at=old_time + timedelta(seconds=1),
    )
    await database.insert_span(span)

    cached = await database.get_cached_completion(cache_key, max_age=timedelta(hours=1))

    assert cached is None


# ---------------------------------------------------------------------------
# Circuit breaker behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_consecutive_failures(clickhouse_settings: Settings) -> None:
    from unittest.mock import AsyncMock, patch

    db = ClickHouseDatabase(settings=clickhouse_settings)

    with patch(
        "ai_pipeline_core.database.clickhouse._backend.get_async_clickhouse_client",
        new_callable=AsyncMock,
        side_effect=ConnectionError("mock connection refused"),
    ):
        for _ in range(3):
            with pytest.raises(ConnectionError, match="mock connection refused"):
                db._client = None  # force reconnect each time
                await db.get_span(uuid4())

    assert db._circuit_open is True
    with pytest.raises(ConnectionError, match="circuit breaker"):
        await db.get_span(uuid4())


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_closes_client(database: ClickHouseDatabase) -> None:
    await database.list_deployments(1)
    assert database._client is not None

    await database.shutdown()

    assert database._client is None


# ---------------------------------------------------------------------------
# Empty inputs and batch short-circuits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_batch_operations(database: ClickHouseDatabase) -> None:
    """Empty batch inserts and queries are no-ops, not errors."""
    await database.save_document_batch([])
    await database.save_blob_batch([])
    await database.save_logs_batch([])
    await database.flush()

    assert await database.get_documents_batch([]) == {}
    assert await database.get_blobs_batch([]) == {}
    assert await database.get_deployment_logs_batch([]) == []
    assert await database.get_deployment_span_count(uuid4(), kinds=[]) == 0
    assert await database.get_spans_referencing_document("sha", kinds=[]) == []


# ---------------------------------------------------------------------------
# None/empty result paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_not_found_returns(database: ClickHouseDatabase) -> None:
    """All get_* methods return None or empty for nonexistent keys."""
    missing = uuid4()

    assert await database.get_span(missing) is None
    assert await database.get_deployment_by_run_id("nonexistent-run") is None
    assert await database.get_document("missing-sha") is None
    assert await database.get_blob("missing-sha") is None
    assert await database.get_cached_completion("missing-key") is None

    assert await database.get_child_spans(missing) == []
    assert await database.get_deployment_tree(missing) == []
    assert await database.get_span_logs(missing) == []
    assert await database.get_deployment_logs(missing) == []
    assert await database.get_all_document_shas_for_tree(missing) == set()
    assert await database.get_spans_referencing_document("missing-sha") == []

    totals = await database.get_deployment_cost_totals(missing)
    assert totals.cost_usd == 0.0
    assert totals.tokens_input == 0

    assert await database.get_deployment_span_count(missing) == 0


# ---------------------------------------------------------------------------
# list_deployments edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_deployments_zero_limit(database: ClickHouseDatabase) -> None:
    assert await database.list_deployments(0) == []
    assert await database.list_deployments(-1) == []


@pytest.mark.asyncio
async def test_list_deployments_without_status_filter(database: ClickHouseDatabase) -> None:
    dep_a = uuid4()
    dep_b = uuid4()
    prefix = f"no-filter-{uuid4().hex[:8]}"
    await database.insert_span(
        _make_span(
            span_id=dep_a,
            deployment_id=dep_a,
            root_deployment_id=dep_a,
            kind=SpanKind.DEPLOYMENT,
            status=SpanStatus.COMPLETED,
            run_id=f"{prefix}-a",
        )
    )
    await database.insert_span(
        _make_span(
            span_id=dep_b,
            deployment_id=dep_b,
            root_deployment_id=dep_b,
            kind=SpanKind.DEPLOYMENT,
            status=SpanStatus.FAILED,
            run_id=f"{prefix}-b",
            started_at=_BASE_TIME + timedelta(seconds=1),
        )
    )

    results = await database.list_deployments(100, status=None)
    ids = {s.span_id for s in results}

    assert dep_a in ids
    assert dep_b in ids


@pytest.mark.asyncio
async def test_list_deployments_root_only_excludes_child_deployments(database: ClickHouseDatabase) -> None:
    root_dep = uuid4()
    child_dep = uuid4()
    prefix = f"root-only-{uuid4().hex[:8]}"
    await database.insert_span(
        _make_span(
            span_id=root_dep,
            deployment_id=root_dep,
            root_deployment_id=root_dep,
            kind=SpanKind.DEPLOYMENT,
            run_id=f"{prefix}-root",
            started_at=_BASE_TIME,
        )
    )
    await database.insert_span(
        _make_span(
            span_id=child_dep,
            parent_span_id=uuid4(),
            deployment_id=child_dep,
            root_deployment_id=root_dep,
            kind=SpanKind.DEPLOYMENT,
            run_id=f"{prefix}-child",
            started_at=_BASE_TIME + timedelta(seconds=1),
        )
    )

    results = await database.list_deployments(100, root_only=True)
    ids = {span.span_id for span in results}

    assert root_dep in ids
    assert child_dep not in ids


@pytest.mark.asyncio
async def test_list_deployments_by_run_id_returns_all_attempts_newest_first(database: ClickHouseDatabase) -> None:
    run_id = f"rerun-{uuid4().hex[:8]}"
    older = uuid4()
    newer = uuid4()
    await database.insert_span(
        _make_span(
            span_id=older,
            deployment_id=older,
            root_deployment_id=older,
            kind=SpanKind.DEPLOYMENT,
            run_id=run_id,
            started_at=_BASE_TIME,
        )
    )
    await database.insert_span(
        _make_span(
            span_id=newer,
            deployment_id=newer,
            root_deployment_id=newer,
            kind=SpanKind.DEPLOYMENT,
            run_id=run_id,
            started_at=_BASE_TIME + timedelta(seconds=5),
        )
    )

    results = await database.list_deployments_by_run_id(run_id)

    assert [span.span_id for span in results[:2]] == [newer, older]


@pytest.mark.asyncio
async def test_list_deployments_respects_limit(database: ClickHouseDatabase) -> None:
    ids = [uuid4() for _ in range(3)]
    prefix = f"limit-{uuid4().hex[:8]}"
    for i, dep_id in enumerate(ids):
        await database.insert_span(
            _make_span(
                span_id=dep_id,
                deployment_id=dep_id,
                root_deployment_id=dep_id,
                kind=SpanKind.DEPLOYMENT,
                run_id=f"{prefix}-{i}",
                started_at=_BASE_TIME + timedelta(seconds=i),
            )
        )

    results = await database.list_deployments(2)

    assert len(results) <= 2


# ---------------------------------------------------------------------------
# get_spans_referencing_document — all 4 array paths + kinds filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_spans_referencing_document_via_all_sha_arrays(database: ClickHouseDatabase) -> None:
    dep_id = uuid4()
    run_id = f"ref-all-{uuid4().hex[:8]}"
    target = f"target-{uuid4().hex[:8]}"

    span_output = _make_span(
        deployment_id=dep_id,
        root_deployment_id=dep_id,
        run_id=run_id,
        output_document_shas=(target,),
        sequence_no=0,
    )
    span_input_blob = _make_span(
        deployment_id=dep_id,
        root_deployment_id=dep_id,
        run_id=run_id,
        input_blob_shas=(target,),
        sequence_no=1,
        started_at=_BASE_TIME + timedelta(seconds=1),
    )
    span_output_blob = _make_span(
        deployment_id=dep_id,
        root_deployment_id=dep_id,
        run_id=run_id,
        output_blob_shas=(target,),
        sequence_no=2,
        started_at=_BASE_TIME + timedelta(seconds=2),
    )
    for s in (span_output, span_input_blob, span_output_blob):
        await database.insert_span(s)

    refs = await database.get_spans_referencing_document(target)

    assert [ref.span_id for ref in refs] == [
        span_output.span_id,
        span_input_blob.span_id,
        span_output_blob.span_id,
    ]
    assert all(ref.input_json == "" for ref in refs)
    assert all(ref.output_json == "" for ref in refs)
    assert all(ref.error_message == "" for ref in refs)


@pytest.mark.asyncio
async def test_get_spans_referencing_document_with_kinds_filter(database: ClickHouseDatabase) -> None:
    dep_id = uuid4()
    run_id = f"ref-kind-{uuid4().hex[:8]}"
    target = f"kf-{uuid4().hex[:8]}"

    task_span = _make_span(
        deployment_id=dep_id,
        root_deployment_id=dep_id,
        run_id=run_id,
        kind=SpanKind.TASK,
        input_document_shas=(target,),
    )
    flow_span = _make_span(
        deployment_id=dep_id,
        root_deployment_id=dep_id,
        run_id=run_id,
        kind=SpanKind.FLOW,
        input_document_shas=(target,),
        started_at=_BASE_TIME + timedelta(seconds=1),
    )
    await database.insert_span(task_span)
    await database.insert_span(flow_span)

    refs = await database.get_spans_referencing_document(target, kinds=[SpanKind.TASK])

    assert len(refs) == 1
    assert refs[0].span_id == task_span.span_id


# ---------------------------------------------------------------------------
# get_document_with_content — failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_document_with_content_missing_primary_blob(database: ClickHouseDatabase) -> None:
    doc = _make_document(content_sha256="no-such-primary-blob")
    await database.save_document(doc)

    result = await database.get_document_with_content(doc.document_sha256)

    assert result is None


@pytest.mark.asyncio
async def test_get_document_with_content_missing_attachment_blob(database: ClickHouseDatabase) -> None:
    primary_sha = f"pri-{uuid4().hex[:8]}"
    att_sha = f"att-missing-{uuid4().hex[:8]}"
    doc = _make_document(
        content_sha256=primary_sha,
        attachment_names=("file.txt",),
        attachment_descriptions=("desc",),
        attachment_content_sha256s=(att_sha,),
        attachment_mime_types=("text/plain",),
        attachment_size_bytes=(5,),
    )
    await database.save_document(doc)
    await database.save_blob(_make_blob(content_sha256=primary_sha, content=b"data"))

    with pytest.raises(ValueError, match="missing from storage"):
        await database.get_document_with_content(doc.document_sha256)


@pytest.mark.asyncio
async def test_get_document_with_content_no_attachments(database: ClickHouseDatabase) -> None:
    content_sha = f"blob-simple-{uuid4().hex[:8]}"
    doc = _make_document(content_sha256=content_sha)
    await database.save_document(doc)
    await database.save_blob(_make_blob(content_sha256=content_sha, content=b"simple"))

    hydrated = await database.get_document_with_content(doc.document_sha256)

    assert hydrated is not None
    assert hydrated.content == b"simple"
    assert hydrated.attachment_contents == {}


# ---------------------------------------------------------------------------
# get_cached_completion — additional paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_cached_completion_without_max_age(database: ClickHouseDatabase) -> None:
    """max_age=None skips time filtering — even very old entries are returned."""
    cache_key = f"no-age-{uuid4().hex[:8]}"
    old_time = datetime(2000, 1, 1, tzinfo=UTC)
    span = _make_span(
        kind=SpanKind.CONVERSATION,
        cache_key=cache_key,
        status=SpanStatus.COMPLETED,
        started_at=old_time,
        ended_at=old_time + timedelta(seconds=1),
    )
    await database.insert_span(span)

    cached = await database.get_cached_completion(cache_key, max_age=None)

    assert cached is not None
    assert cached.span_id == span.span_id


# ---------------------------------------------------------------------------
# get_deployment_cost_totals — edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_deployment_cost_totals_no_llm_rounds(database: ClickHouseDatabase) -> None:
    """Tree with only deployment and task spans — no LLM_ROUND — returns zeros."""
    root_id = uuid4()
    run_id = f"no-rounds-{uuid4().hex[:8]}"
    await database.insert_span(
        _make_span(
            span_id=root_id,
            deployment_id=root_id,
            root_deployment_id=root_id,
            kind=SpanKind.DEPLOYMENT,
            run_id=run_id,
        )
    )
    await database.insert_span(
        _make_span(
            parent_span_id=root_id,
            deployment_id=root_id,
            root_deployment_id=root_id,
            kind=SpanKind.TASK,
            run_id=run_id,
            started_at=_BASE_TIME + timedelta(seconds=1),
        )
    )

    totals = await database.get_deployment_cost_totals(root_id)

    assert totals == CostTotals()


@pytest.mark.asyncio
async def test_get_deployment_cost_totals_empty_metrics_json(database: ClickHouseDatabase) -> None:
    """LLM_ROUND with empty metrics_json — cost aggregated, tokens all zero."""
    root_id = uuid4()
    run_id = f"empty-metrics-{uuid4().hex[:8]}"
    await database.insert_span(
        _make_span(
            span_id=root_id,
            deployment_id=root_id,
            root_deployment_id=root_id,
            kind=SpanKind.DEPLOYMENT,
            run_id=run_id,
        )
    )
    await database.insert_span(
        _make_span(
            parent_span_id=root_id,
            deployment_id=root_id,
            root_deployment_id=root_id,
            kind=SpanKind.LLM_ROUND,
            run_id=run_id,
            cost_usd=0.1,
            metrics_json="{}",
            started_at=_BASE_TIME + timedelta(seconds=1),
        )
    )

    totals = await database.get_deployment_cost_totals(root_id)

    assert totals.cost_usd == pytest.approx(0.1)
    assert totals.tokens_input == 0
    assert totals.tokens_output == 0


# ---------------------------------------------------------------------------
# Log filtering — category and combined filters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_filters_by_category_and_combined(database: ClickHouseDatabase) -> None:
    deployment_id = uuid4()
    span_id = uuid4()
    await database.save_logs_batch([
        _make_log(deployment_id=deployment_id, span_id=span_id, level="INFO", category="auth", message="a", sequence_no=0),
        _make_log(deployment_id=deployment_id, span_id=span_id, level="ERROR", category="auth", message="b", sequence_no=1),
        _make_log(deployment_id=deployment_id, span_id=span_id, level="INFO", category="db", message="c", sequence_no=2),
    ])

    # Category only
    auth_logs = await database.get_span_logs(span_id, category="auth")
    assert len(auth_logs) == 2

    # Combined level + category
    err_auth = await database.get_span_logs(span_id, level="ERROR", category="auth")
    assert len(err_auth) == 1
    assert err_auth[0].message == "b"

    # Deployment-level category filter
    dep_auth = await database.get_deployment_logs(deployment_id, category="auth")
    assert len(dep_auth) == 2

    # Deployment-level level filter
    dep_errors = await database.get_deployment_logs(deployment_id, level="ERROR")
    assert len(dep_errors) == 1

    # Batch with level filter
    batch_errors = await database.get_deployment_logs_batch([deployment_id], level="ERROR")
    assert len(batch_errors) == 1


# ---------------------------------------------------------------------------
# Batch partial matches and deduplication
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_documents_batch_partial_match(database: ClickHouseDatabase) -> None:
    doc = _make_document()
    await database.save_document(doc)

    batch = await database.get_documents_batch([doc.document_sha256, "nonexistent"])

    assert len(batch) == 1
    assert doc.document_sha256 in batch


@pytest.mark.asyncio
async def test_get_blobs_batch_partial_match(database: ClickHouseDatabase) -> None:
    blob = _make_blob()
    await database.save_blob(blob)

    batch = await database.get_blobs_batch([blob.content_sha256, "nonexistent"])

    assert len(batch) == 1
    assert blob.content_sha256 in batch


# ---------------------------------------------------------------------------
# Full field roundtrip fidelity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_span_roundtrip_preserves_all_fields(database: ClickHouseDatabase) -> None:
    """Every SpanRecord field survives insert → SELECT → row_to_span."""
    prev_conv_id = uuid4()
    span = _make_span(
        deployment_name="roundtrip-deploy",
        name="RoundtripTask",
        description="full field test",
        status=SpanStatus.FAILED,
        cache_key="rk-123",
        previous_conversation_id=prev_conv_id,
        cost_usd=1.23,
        error_type="ValueError",
        error_message="something broke",
        input_document_shas=("in-sha-1", "in-sha-2"),
        output_document_shas=("out-sha-1",),
        target="gpt-4",
        receiver_json='{"model": "gpt-4"}',
        input_json='{"prompt": "hello"}',
        output_json='{"text": "world"}',
        error_json='{"code": 500}',
        meta_json='{"retries": 2}',
        metrics_json='{"tokens_input": 100}',
        input_blob_shas=("blob-in-1",),
        output_blob_shas=("blob-out-1", "blob-out-2"),
    )
    await database.insert_span(span)

    loaded = await database.get_span(span.span_id)

    assert loaded is not None
    assert loaded.deployment_name == "roundtrip-deploy"
    assert loaded.description == "full field test"
    assert loaded.status == SpanStatus.FAILED
    assert loaded.cache_key == "rk-123"
    assert loaded.previous_conversation_id == prev_conv_id
    assert loaded.cost_usd == pytest.approx(1.23)
    assert loaded.error_type == "ValueError"
    assert loaded.error_message == "something broke"
    assert loaded.input_document_shas == ("in-sha-1", "in-sha-2")
    assert loaded.output_document_shas == ("out-sha-1",)
    assert loaded.target == "gpt-4"
    assert loaded.receiver_json == '{"model": "gpt-4"}'
    assert loaded.input_json == '{"prompt": "hello"}'
    assert loaded.output_json == '{"text": "world"}'
    assert loaded.error_json == '{"code": 500}'
    assert loaded.meta_json == '{"retries": 2}'
    assert loaded.metrics_json == '{"tokens_input": 100}'
    assert loaded.input_blob_shas == ("blob-in-1",)
    assert loaded.output_blob_shas == ("blob-out-1", "blob-out-2")


@pytest.mark.asyncio
async def test_document_roundtrip_preserves_all_fields(database: ClickHouseDatabase) -> None:
    """Every DocumentRecord field survives insert → SELECT → row_to_document."""
    doc = _make_document(
        document_type="SpecialDoc",
        name="report.pdf",
        description="Quarterly report",
        mime_type="application/pdf",
        size_bytes=4096,
        summary="A quarterly summary",
        derived_from=("parent-sha-1", "parent-sha-2"),
        triggered_by=("trigger-sha-1",),
        attachment_names=("chart.png", "data.csv"),
        attachment_descriptions=("Bar chart", "Raw data"),
        attachment_content_sha256s=("att-sha-1", "att-sha-2"),
        attachment_mime_types=("image/png", "text/csv"),
        attachment_size_bytes=(2048, 512),
    )
    await database.save_document(doc)

    loaded = await database.get_document(doc.document_sha256)

    assert loaded is not None
    assert loaded.document_type == "SpecialDoc"
    assert loaded.name == "report.pdf"
    assert loaded.description == "Quarterly report"
    assert loaded.mime_type == "application/pdf"
    assert loaded.size_bytes == 4096
    assert loaded.summary == "A quarterly summary"
    assert loaded.derived_from == ("parent-sha-1", "parent-sha-2")
    assert loaded.triggered_by == ("trigger-sha-1",)
    assert loaded.attachment_names == ("chart.png", "data.csv")
    assert loaded.attachment_descriptions == ("Bar chart", "Raw data")
    assert loaded.attachment_content_sha256s == ("att-sha-1", "att-sha-2")
    assert loaded.attachment_mime_types == ("image/png", "text/csv")
    assert loaded.attachment_size_bytes == (2048, 512)


@pytest.mark.asyncio
async def test_update_document_summary_nonexistent_is_noop(database: ClickHouseDatabase) -> None:
    """Updating summary for a nonexistent document does not raise."""
    await database.update_document_summary("nonexistent-sha", "ignored")
    assert await database.get_document("nonexistent-sha") is None


# ---------------------------------------------------------------------------
# publicly_visible field
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_document_publicly_visible_roundtrip(database: ClickHouseDatabase) -> None:
    """publicly_visible=True persists and round-trips through ClickHouse."""
    doc = _make_document(
        document_sha256=f"pub-vis-{uuid4().hex[:12]}",
        publicly_visible=True,
    )
    await database.save_document(doc)
    loaded = await database.get_document(doc.document_sha256)
    assert loaded is not None
    assert loaded.publicly_visible is True


@pytest.mark.asyncio
async def test_document_publicly_visible_default_false(database: ClickHouseDatabase) -> None:
    """Default publicly_visible=False persists correctly."""
    doc = _make_document(document_sha256=f"pub-def-{uuid4().hex[:12]}")
    await database.save_document(doc)
    loaded = await database.get_document(doc.document_sha256)
    assert loaded is not None
    assert loaded.publicly_visible is False


@pytest.mark.asyncio
async def test_document_publicly_visible_survives_summary_update(database: ClickHouseDatabase) -> None:
    """update_document_summary preserves publicly_visible."""
    doc = _make_document(
        document_sha256=f"pub-upd-{uuid4().hex[:12]}",
        publicly_visible=True,
    )
    await database.save_document(doc)
    await database.update_document_summary(doc.document_sha256, "new summary")
    loaded = await database.get_document(doc.document_sha256)
    assert loaded is not None
    assert loaded.publicly_visible is True
    assert loaded.summary == "new summary"


# ---------------------------------------------------------------------------
# find_documents_by_name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_documents_by_name_returns_matching_records(database: ClickHouseDatabase) -> None:
    suffix = uuid4().hex[:8]
    doc_a = _make_document(document_sha256=f"fname-a-{suffix}", name=f"report-{suffix}.md", document_type="Report")
    doc_b = _make_document(document_sha256=f"fname-b-{suffix}", name=f"summary-{suffix}.md", document_type="Summary")
    doc_c = _make_document(document_sha256=f"fname-c-{suffix}", name=f"other-{suffix}.md", document_type="Report")
    for doc in (doc_a, doc_b, doc_c):
        await database.save_document(doc)

    found = await database.find_documents_by_name([f"report-{suffix}.md", f"summary-{suffix}.md"])

    assert set(found.keys()) == {f"report-{suffix}.md", f"summary-{suffix}.md"}
    assert found[f"report-{suffix}.md"].document_sha256 == f"fname-a-{suffix}"
    assert found[f"summary-{suffix}.md"].document_sha256 == f"fname-b-{suffix}"


@pytest.mark.asyncio
async def test_find_documents_by_name_filters_by_document_type(database: ClickHouseDatabase) -> None:
    suffix = uuid4().hex[:8]
    doc_a = _make_document(document_sha256=f"ftype-a-{suffix}", name=f"report-{suffix}.md", document_type="Report")
    doc_b = _make_document(document_sha256=f"ftype-b-{suffix}", name=f"report-{suffix}.md", document_type="Draft")
    for doc in (doc_a, doc_b):
        await database.save_document(doc)

    found = await database.find_documents_by_name([f"report-{suffix}.md"], document_type="Draft")

    assert found[f"report-{suffix}.md"].document_sha256 == f"ftype-b-{suffix}"


@pytest.mark.asyncio
async def test_find_documents_by_name_tiebreaks_by_highest_sha256(database: ClickHouseDatabase) -> None:
    suffix = uuid4().hex[:8]
    doc_low = _make_document(document_sha256=f"aaa-{suffix}", name=f"tie-{suffix}.md")
    doc_high = _make_document(document_sha256=f"zzz-{suffix}", name=f"tie-{suffix}.md")
    for doc in (doc_low, doc_high):
        await database.save_document(doc)

    found = await database.find_documents_by_name([f"tie-{suffix}.md"])

    assert found[f"tie-{suffix}.md"].document_sha256 == f"zzz-{suffix}"


@pytest.mark.asyncio
async def test_find_documents_by_name_empty_names_returns_empty(database: ClickHouseDatabase) -> None:
    found = await database.find_documents_by_name([])

    assert found == {}


@pytest.mark.asyncio
async def test_find_documents_by_name_no_matches_returns_empty(database: ClickHouseDatabase) -> None:
    found = await database.find_documents_by_name(["nonexistent-doc-name.md"])

    assert found == {}
