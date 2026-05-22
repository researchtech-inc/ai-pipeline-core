"""Tests for _reconstruct_lifecycle_events."""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from ai_pipeline_core.database import DocumentRecord, SpanKind, SpanRecord, SpanStatus
from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.deployment._event_serialization import event_to_payload
from ai_pipeline_core.deployment._event_reconstruction import (
    InvalidLifecycleCursorError,
    _ReconstructedEvent,
    _filter_after_cursor,
    _reconstruct_lifecycle_events,
    reconstructed_event_cursor,
)
from ai_pipeline_core.deployment._types import ErrorCode, EventType


def _make_span(**kwargs: object) -> SpanRecord:
    deployment_id = kwargs.pop("deployment_id", uuid4())
    root_deployment_id = kwargs.pop("root_deployment_id", deployment_id)
    started_at: datetime = kwargs.pop("started_at", datetime(2026, 3, 14, 12, 0, tzinfo=UTC))
    defaults: dict[str, object] = {
        "span_id": kwargs.pop("span_id", uuid4()),
        "parent_span_id": kwargs.pop("parent_span_id", None),
        "deployment_id": deployment_id,
        "root_deployment_id": root_deployment_id,
        "run_id": kwargs.pop("run_id", "test-run"),
        "deployment_name": "test-deploy",
        "kind": SpanKind.TASK,
        "name": "TestTask",
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
        "name": "test.md",
        "mime_type": "text/markdown",
        "size_bytes": 10,
        "derived_from": (),
        "triggered_by": (),
    }
    defaults.update(kwargs)
    return DocumentRecord(**defaults)


async def _seed_successful_run(db: _MemoryDatabase) -> tuple[UUID, UUID]:
    """Seed a complete deployment → flow → task span tree. Returns (root_deployment_id, deployment_span_id)."""
    root_id = uuid4()
    deploy_id = root_id
    deploy_span_id = uuid4()
    flow_span_id = uuid4()
    task_span_id = uuid4()
    t0 = datetime(2026, 3, 14, 12, 0, tzinfo=UTC)

    doc = _make_document(document_sha256="out-sha-1", publicly_visible=True)
    await db.save_document(doc)

    deploy = _make_span(
        span_id=deploy_span_id,
        deployment_id=deploy_id,
        root_deployment_id=root_id,
        kind=SpanKind.DEPLOYMENT,
        name="TestDeploy",
        status=SpanStatus.COMPLETED,
        started_at=t0,
        ended_at=t0 + timedelta(seconds=10),
        meta_json=json.dumps({
            "input_fingerprint": "abc123",
            "flow_plan": [{"name": "Flow1", "flow_class": "TestFlow", "step": 1, "expected_tasks": [{"name": "TestTask", "estimated_minutes": 1.0}]}],
            "deployment_class": "TestPipeline",
        }),
        output_json=json.dumps({"result": {"ok": True}}),
        output_document_shas=("out-sha-1",),
    )
    flow = _make_span(
        span_id=flow_span_id,
        parent_span_id=deploy_span_id,
        deployment_id=deploy_id,
        root_deployment_id=root_id,
        kind=SpanKind.FLOW,
        name="Flow1",
        status=SpanStatus.COMPLETED,
        started_at=t0 + timedelta(seconds=1),
        ended_at=t0 + timedelta(seconds=8),
        target="classmethod:my.mod:TestFlow.run",
        meta_json=json.dumps({
            "step": 1,
            "total_steps": 1,
            "expected_tasks": [{"name": "TestTask", "estimated_minutes": 1.0}],
        }),
        receiver_json=json.dumps({"mode": "constructor_args", "value": {"param": "val"}}),
        output_document_shas=("out-sha-1",),
    )
    task = _make_span(
        span_id=task_span_id,
        parent_span_id=flow_span_id,
        deployment_id=deploy_id,
        root_deployment_id=root_id,
        kind=SpanKind.TASK,
        name="TestTask",
        status=SpanStatus.COMPLETED,
        started_at=t0 + timedelta(seconds=2),
        ended_at=t0 + timedelta(seconds=5),
        target="classmethod:my.mod:TestTask.run",
        output_document_shas=("out-sha-1",),
    )
    for span in (deploy, flow, task):
        await db.insert_span(span)

    return root_id, deploy_span_id


@pytest.mark.asyncio
async def test_successful_run_reconstruction() -> None:
    db = _MemoryDatabase()
    root_id, _ = await _seed_successful_run(db)

    events = await _reconstruct_lifecycle_events(db, root_id)

    event_types = [e.event_type for e in events]
    assert EventType.RUN_STARTED in event_types
    assert EventType.FLOW_STARTED in event_types
    assert EventType.TASK_STARTED in event_types
    assert EventType.TASK_COMPLETED in event_types
    assert EventType.FLOW_COMPLETED in event_types
    assert EventType.RUN_COMPLETED in event_types

    run_started = next(e for e in events if e.event_type == EventType.RUN_STARTED)
    assert run_started.data["deployment_class"] == "TestPipeline"
    assert run_started.data["input_fingerprint"] == "abc123"
    assert len(run_started.data["flow_plan"]) == 1

    run_completed = next(e for e in events if e.event_type == EventType.RUN_COMPLETED)
    assert run_completed.data["result"] == {"ok": True}
    assert run_completed.data["duration_ms"] == 10000

    flow_started = next(e for e in events if e.event_type == EventType.FLOW_STARTED)
    assert flow_started.data["flow_name"] == "Flow1"
    assert flow_started.data["flow_params"] == {"param": "val"}
    assert flow_started.data["expected_tasks"] == [{"name": "TestTask", "estimated_minutes": 1.0}]

    flow_completed = next(e for e in events if e.event_type == EventType.FLOW_COMPLETED)
    assert len(flow_completed.data["output_documents"]) == 1
    assert flow_completed.data["output_documents"][0]["publicly_visible"] is True


@pytest.mark.asyncio
async def test_child_deployment_events_use_child_parent_links() -> None:
    db = _MemoryDatabase()
    root_id = uuid4()
    root_deploy_span_id = root_id
    root_task_id = uuid4()
    child_deploy_span_id = uuid4()
    child_flow_id = uuid4()
    child_task_id = uuid4()
    t0 = datetime(2026, 3, 14, 12, 0, tzinfo=UTC)

    root_deploy = _make_span(
        span_id=root_deploy_span_id,
        deployment_id=root_id,
        root_deployment_id=root_id,
        kind=SpanKind.DEPLOYMENT,
        name="RootDeploy",
        run_id="root-run",
        status=SpanStatus.COMPLETED,
        started_at=t0,
        ended_at=t0 + timedelta(seconds=20),
        meta_json=json.dumps({"input_fingerprint": "fp", "deployment_class": "RootPipeline", "flow_plan": []}),
        output_json=json.dumps({"result": {}}),
    )
    root_task = _make_span(
        span_id=root_task_id,
        parent_span_id=root_deploy_span_id,
        deployment_id=root_id,
        root_deployment_id=root_id,
        kind=SpanKind.TASK,
        name="remote:child",
        run_id="root-run",
        status=SpanStatus.COMPLETED,
        started_at=t0 + timedelta(seconds=1),
        ended_at=t0 + timedelta(seconds=3),
    )
    child_deploy = _make_span(
        span_id=child_deploy_span_id,
        parent_span_id=root_task_id,
        deployment_id=child_deploy_span_id,
        root_deployment_id=root_id,
        kind=SpanKind.DEPLOYMENT,
        name="ChildDeploy",
        run_id="child-run",
        status=SpanStatus.COMPLETED,
        started_at=t0 + timedelta(seconds=4),
        ended_at=t0 + timedelta(seconds=12),
        meta_json=json.dumps({"input_fingerprint": "child-fp", "deployment_class": "ChildPipeline", "flow_plan": []}),
        output_json=json.dumps({"result": {}}),
    )
    child_flow = _make_span(
        span_id=child_flow_id,
        parent_span_id=child_deploy_span_id,
        deployment_id=child_deploy_span_id,
        root_deployment_id=root_id,
        kind=SpanKind.FLOW,
        name="ChildFlow",
        run_id="child-run",
        status=SpanStatus.COMPLETED,
        started_at=t0 + timedelta(seconds=5),
        ended_at=t0 + timedelta(seconds=10),
        meta_json=json.dumps({"step": 1, "total_steps": 1, "expected_tasks": []}),
        receiver_json=json.dumps({"mode": "constructor_args", "value": {}}),
    )
    child_task = _make_span(
        span_id=child_task_id,
        parent_span_id=child_flow_id,
        deployment_id=child_deploy_span_id,
        root_deployment_id=root_id,
        kind=SpanKind.TASK,
        name="ChildTask",
        run_id="child-run",
        status=SpanStatus.COMPLETED,
        started_at=t0 + timedelta(seconds=6),
        ended_at=t0 + timedelta(seconds=9),
    )

    for span in (root_deploy, root_task, child_deploy, child_flow, child_task):
        await db.insert_span(span)

    events = await _reconstruct_lifecycle_events(db, root_id)

    child_run_started = next(e for e in events if e.event_type == EventType.RUN_STARTED and e.data["run_id"] == "child-run")
    child_flow_started = next(e for e in events if e.event_type == EventType.FLOW_STARTED and e.data["run_id"] == "child-run")
    child_task_started = next(e for e in events if e.event_type == EventType.TASK_STARTED and e.data["run_id"] == "child-run")

    assert child_run_started.data["parent_deployment_task_id"] == str(root_task_id)
    assert child_flow_started.data["parent_deployment_task_id"] == str(root_task_id)
    assert child_flow_started.data["parent_span_id"] == str(child_deploy_span_id)
    assert child_task_started.data["parent_deployment_task_id"] == str(root_task_id)


@pytest.mark.asyncio
async def test_in_progress_run() -> None:
    db = _MemoryDatabase()
    root_id = uuid4()
    t0 = datetime(2026, 3, 14, 12, 0, tzinfo=UTC)

    deploy = _make_span(
        span_id=uuid4(),
        deployment_id=root_id,
        root_deployment_id=root_id,
        kind=SpanKind.DEPLOYMENT,
        name="RunningDeploy",
        status=SpanStatus.RUNNING,
        started_at=t0,
        ended_at=None,
        meta_json=json.dumps({"input_fingerprint": "fp", "deployment_class": "P", "flow_plan": []}),
    )
    await db.insert_span(deploy)

    events = await _reconstruct_lifecycle_events(db, root_id)
    event_types = [e.event_type for e in events]
    assert event_types == [EventType.RUN_STARTED]


@pytest.mark.asyncio
async def test_failed_run_with_error_code() -> None:
    db = _MemoryDatabase()
    root_id = uuid4()
    t0 = datetime(2026, 3, 14, 12, 0, tzinfo=UTC)

    deploy = _make_span(
        span_id=uuid4(),
        deployment_id=root_id,
        root_deployment_id=root_id,
        kind=SpanKind.DEPLOYMENT,
        name="FailedDeploy",
        status=SpanStatus.FAILED,
        started_at=t0,
        ended_at=t0 + timedelta(seconds=5),
        error_message="boom",
        meta_json=json.dumps({"input_fingerprint": "fp", "deployment_class": "P", "flow_plan": [], "error_code": "provider_error"}),
    )
    await db.insert_span(deploy)

    events = await _reconstruct_lifecycle_events(db, root_id)
    run_failed = next(e for e in events if e.event_type == EventType.RUN_FAILED)
    assert run_failed.data["error_code"] == "provider_error"
    assert run_failed.data["error_message"] == "boom"


@pytest.mark.asyncio
async def test_skipped_flow() -> None:
    db = _MemoryDatabase()
    root_id = uuid4()
    deploy_span_id = uuid4()
    t0 = datetime(2026, 3, 14, 12, 0, tzinfo=UTC)

    deploy = _make_span(
        span_id=deploy_span_id,
        deployment_id=root_id,
        root_deployment_id=root_id,
        kind=SpanKind.DEPLOYMENT,
        name="Deploy",
        status=SpanStatus.COMPLETED,
        started_at=t0,
        ended_at=t0 + timedelta(seconds=5),
        meta_json=json.dumps({"input_fingerprint": "fp", "deployment_class": "P", "flow_plan": []}),
        output_json=json.dumps({"result": {}}),
    )
    flow = _make_span(
        span_id=uuid4(),
        parent_span_id=deploy_span_id,
        deployment_id=root_id,
        root_deployment_id=root_id,
        kind=SpanKind.FLOW,
        name="SkippedFlow",
        status=SpanStatus.SKIPPED,
        started_at=t0 + timedelta(seconds=1),
        ended_at=t0 + timedelta(seconds=1),
        meta_json=json.dumps({"step": 1, "total_steps": 2, "skip_reason": "resumed_past_step"}),
    )
    for span in (deploy, flow):
        await db.insert_span(span)

    events = await _reconstruct_lifecycle_events(db, root_id)
    skipped = next(e for e in events if e.event_type == EventType.FLOW_SKIPPED)
    assert skipped.data["reason"] == "resumed_past_step"
    assert skipped.data["status"] == "skipped"

    flow_started_events = [e for e in events if e.event_type == EventType.FLOW_STARTED]
    assert len(flow_started_events) == 0


@pytest.mark.asyncio
async def test_cached_flow() -> None:
    db = _MemoryDatabase()
    root_id = uuid4()
    deploy_span_id = uuid4()
    t0 = datetime(2026, 3, 14, 12, 0, tzinfo=UTC)

    deploy = _make_span(
        span_id=deploy_span_id,
        deployment_id=root_id,
        root_deployment_id=root_id,
        kind=SpanKind.DEPLOYMENT,
        name="Deploy",
        status=SpanStatus.COMPLETED,
        started_at=t0,
        ended_at=t0 + timedelta(seconds=5),
        meta_json=json.dumps({"input_fingerprint": "fp", "deployment_class": "P", "flow_plan": []}),
        output_json=json.dumps({"result": {}}),
    )
    flow = _make_span(
        span_id=uuid4(),
        parent_span_id=deploy_span_id,
        deployment_id=root_id,
        root_deployment_id=root_id,
        kind=SpanKind.FLOW,
        name="CachedFlow",
        status=SpanStatus.CACHED,
        started_at=t0 + timedelta(seconds=1),
        ended_at=t0 + timedelta(seconds=1),
        meta_json=json.dumps({"step": 1, "total_steps": 1, "skip_reason": "cached_result_available"}),
    )
    for span in (deploy, flow):
        await db.insert_span(span)

    events = await _reconstruct_lifecycle_events(db, root_id)
    skipped = next(e for e in events if e.event_type == EventType.FLOW_SKIPPED)
    assert skipped.data["status"] == "cached"
    assert skipped.data["reason"] == "cached_result_available"


@pytest.mark.asyncio
async def test_cached_task_no_started_event() -> None:
    db = _MemoryDatabase()
    root_id = uuid4()
    deploy_span_id = uuid4()
    flow_span_id = uuid4()
    t0 = datetime(2026, 3, 14, 12, 0, tzinfo=UTC)

    deploy = _make_span(
        span_id=deploy_span_id,
        deployment_id=root_id,
        root_deployment_id=root_id,
        kind=SpanKind.DEPLOYMENT,
        name="Deploy",
        status=SpanStatus.COMPLETED,
        started_at=t0,
        ended_at=t0 + timedelta(seconds=5),
        meta_json=json.dumps({"input_fingerprint": "fp", "deployment_class": "P", "flow_plan": []}),
        output_json=json.dumps({"result": {}}),
    )
    flow = _make_span(
        span_id=flow_span_id,
        parent_span_id=deploy_span_id,
        deployment_id=root_id,
        root_deployment_id=root_id,
        kind=SpanKind.FLOW,
        name="Flow1",
        status=SpanStatus.COMPLETED,
        started_at=t0 + timedelta(seconds=1),
        ended_at=t0 + timedelta(seconds=4),
        meta_json=json.dumps({"step": 1, "total_steps": 1}),
    )
    task = _make_span(
        span_id=uuid4(),
        parent_span_id=flow_span_id,
        deployment_id=root_id,
        root_deployment_id=root_id,
        kind=SpanKind.TASK,
        name="CachedTask",
        status=SpanStatus.CACHED,
        started_at=t0 + timedelta(seconds=2),
        ended_at=t0 + timedelta(seconds=3),
    )
    for span in (deploy, flow, task):
        await db.insert_span(span)

    events = await _reconstruct_lifecycle_events(db, root_id)

    task_started = [e for e in events if e.event_type == EventType.TASK_STARTED]
    assert len(task_started) == 0

    task_completed = [e for e in events if e.event_type == EventType.TASK_COMPLETED]
    assert len(task_completed) == 1
    assert task_completed[0].data["status"] == "cached"


@pytest.mark.asyncio
async def test_event_ordering_deterministic() -> None:
    db = _MemoryDatabase()
    root_id, _ = await _seed_successful_run(db)

    events = await _reconstruct_lifecycle_events(db, root_id)
    timestamps = [e.timestamp for e in events]
    assert timestamps == sorted(timestamps)

    types_in_order = [e.event_type for e in events]
    run_started_idx = types_in_order.index(EventType.RUN_STARTED)
    run_completed_idx = types_in_order.index(EventType.RUN_COMPLETED)
    assert run_started_idx < run_completed_idx


@pytest.mark.asyncio
async def test_empty_tree_returns_empty() -> None:
    db = _MemoryDatabase()
    events = await _reconstruct_lifecycle_events(db, uuid4())
    assert events == []


@pytest.mark.asyncio
async def test_operation_spans_are_ignored() -> None:
    db = _MemoryDatabase()
    root_id = uuid4()
    deploy_span_id = uuid4()
    t0 = datetime(2026, 3, 14, 12, 0, tzinfo=UTC)

    deploy = _make_span(
        span_id=deploy_span_id,
        deployment_id=root_id,
        root_deployment_id=root_id,
        kind=SpanKind.DEPLOYMENT,
        name="Deploy",
        status=SpanStatus.RUNNING,
        started_at=t0,
        ended_at=None,
        meta_json=json.dumps({"input_fingerprint": "fp", "deployment_class": "P", "flow_plan": []}),
    )
    operation = _make_span(
        span_id=uuid4(),
        parent_span_id=deploy_span_id,
        deployment_id=root_id,
        root_deployment_id=root_id,
        kind=SpanKind.OPERATION,
        name="SomeOp",
        status=SpanStatus.COMPLETED,
        started_at=t0 + timedelta(seconds=1),
        ended_at=t0 + timedelta(seconds=2),
    )
    for span in (deploy, operation):
        await db.insert_span(span)

    events = await _reconstruct_lifecycle_events(db, root_id)
    for event in events:
        assert "operation" not in event.event_type


@pytest.mark.asyncio
async def test_event_to_payload_strenum_normalized() -> None:
    """Verify that StrEnum values are converted to plain strings."""
    from ai_pipeline_core.deployment._types import RunFailedEvent

    event = RunFailedEvent(
        run_id="r1",
        span_id="s1",
        root_deployment_id="rd1",
        parent_deployment_task_id=None,
        status="failed",
        error_code=ErrorCode.PROVIDER_ERROR,
        error_message="err",
    )
    payload = event_to_payload(event)
    assert isinstance(payload["error_code"], str)
    assert type(payload["error_code"]) is str
    assert payload["error_code"] == "provider_error"


@pytest.mark.asyncio
async def test_document_ref_from_record() -> None:
    from ai_pipeline_core._lifecycle_events import DocumentRef

    record = _make_document(
        document_sha256="sha-abc",
        document_type="MyDoc",
        name="doc.md",
        summary="A doc",
        publicly_visible=True,
        derived_from=("https://a.com",),
        triggered_by=("sha-xyz",),
    )
    ref = DocumentRef.from_record(record)
    assert ref.sha256 == "sha-abc"
    assert ref.class_name == "MyDoc"
    assert ref.name == "doc.md"
    assert ref.summary == "A doc"
    assert ref.publicly_visible is True
    assert ref.derived_from == ("https://a.com",)
    assert ref.triggered_by == ("sha-xyz",)


@pytest.mark.asyncio
async def test_reconstruction_expected_tasks_are_dicts() -> None:
    """Reconstructed flow.started carries expected_tasks as list of dicts."""
    db = _MemoryDatabase()
    root_id, _ = await _seed_successful_run(db)

    events = await _reconstruct_lifecycle_events(db, root_id)
    flow_started = next(e for e in events if e.event_type == EventType.FLOW_STARTED)

    expected = [{"name": "TestTask", "estimated_minutes": 1.0}]
    assert flow_started.data["expected_tasks"] == expected


@pytest.mark.asyncio
async def test_reconstruction_expected_tasks_custom_minutes() -> None:
    """Reconstructed flow.started preserves custom estimated_minutes values."""
    db = _MemoryDatabase()
    root_id = uuid4()
    deploy_span_id = uuid4()
    flow_span_id = uuid4()
    t0 = datetime(2026, 3, 14, 12, 0, tzinfo=UTC)

    expected_tasks = [
        {"name": "TaskA", "estimated_minutes": 3.0},
        {"name": "TaskB", "estimated_minutes": 7.0},
    ]
    flow_plan = [
        {
            "name": "Flow1",
            "flow_class": "MultiTaskFlow",
            "step": 1,
            "estimated_minutes": 10.0,
            "expected_tasks": expected_tasks,
        }
    ]

    deploy = _make_span(
        span_id=deploy_span_id,
        deployment_id=root_id,
        root_deployment_id=root_id,
        kind=SpanKind.DEPLOYMENT,
        name="Deploy",
        status=SpanStatus.COMPLETED,
        started_at=t0,
        ended_at=t0 + timedelta(seconds=10),
        meta_json=json.dumps({
            "input_fingerprint": "fp",
            "deployment_class": "P",
            "flow_plan": flow_plan,
        }),
        output_json=json.dumps({"result": {}}),
    )
    flow = _make_span(
        span_id=flow_span_id,
        parent_span_id=deploy_span_id,
        deployment_id=root_id,
        root_deployment_id=root_id,
        kind=SpanKind.FLOW,
        name="Flow1",
        status=SpanStatus.COMPLETED,
        started_at=t0 + timedelta(seconds=1),
        ended_at=t0 + timedelta(seconds=8),
        target="classmethod:mod:MultiTaskFlow.run",
        meta_json=json.dumps({
            "step": 1,
            "total_steps": 1,
            "expected_tasks": expected_tasks,
        }),
        receiver_json=json.dumps({"mode": "constructor_args", "value": {}}),
    )
    for span in (deploy, flow):
        await db.insert_span(span)

    events = await _reconstruct_lifecycle_events(db, root_id)

    flow_started = next(e for e in events if e.event_type == EventType.FLOW_STARTED)
    assert flow_started.data["expected_tasks"] == expected_tasks

    run_started = next(e for e in events if e.event_type == EventType.RUN_STARTED)
    assert run_started.data["flow_plan"][0]["expected_tasks"] == expected_tasks


@pytest.mark.asyncio
async def test_reconstruction_expected_tasks_empty_list() -> None:
    """Reconstructed flow.started with no expected_tasks returns empty list."""
    db = _MemoryDatabase()
    root_id = uuid4()
    deploy_span_id = uuid4()
    flow_span_id = uuid4()
    t0 = datetime(2026, 3, 14, 12, 0, tzinfo=UTC)

    deploy = _make_span(
        span_id=deploy_span_id,
        deployment_id=root_id,
        root_deployment_id=root_id,
        kind=SpanKind.DEPLOYMENT,
        name="Deploy",
        status=SpanStatus.COMPLETED,
        started_at=t0,
        ended_at=t0 + timedelta(seconds=5),
        meta_json=json.dumps({"input_fingerprint": "fp", "deployment_class": "P", "flow_plan": []}),
        output_json=json.dumps({"result": {}}),
    )
    flow = _make_span(
        span_id=flow_span_id,
        parent_span_id=deploy_span_id,
        deployment_id=root_id,
        root_deployment_id=root_id,
        kind=SpanKind.FLOW,
        name="EmptyTasksFlow",
        status=SpanStatus.COMPLETED,
        started_at=t0 + timedelta(seconds=1),
        ended_at=t0 + timedelta(seconds=4),
        target="classmethod:mod:EmptyFlow.run",
        meta_json=json.dumps({
            "step": 1,
            "total_steps": 1,
            "expected_tasks": [],
        }),
        receiver_json=json.dumps({"mode": "constructor_args", "value": {}}),
    )
    for span in (deploy, flow):
        await db.insert_span(span)

    events = await _reconstruct_lifecycle_events(db, root_id)
    flow_started = next(e for e in events if e.event_type == EventType.FLOW_STARTED)
    assert flow_started.data["expected_tasks"] == []


@pytest.mark.asyncio
async def test_expected_tasks_roundtrip_serialization() -> None:
    """event_to_payload serializes expected_tasks dicts correctly for PubSub."""
    from ai_pipeline_core.deployment._types import FlowStartedEvent

    expected_tasks = [
        {"name": "TaskA", "estimated_minutes": 3.0},
        {"name": "TaskB", "estimated_minutes": 1.0},
    ]
    event = FlowStartedEvent(
        run_id="test-run",
        span_id="span-1",
        root_deployment_id="root-1",
        parent_deployment_task_id=None,
        flow_name="Flow1",
        flow_class="FlowClass",
        step=1,
        total_steps=1,
        status="running",
        expected_tasks=expected_tasks,
    )
    payload = event_to_payload(event)
    assert payload["expected_tasks"] == expected_tasks
    # Verify JSON round-trip
    serialized = json.loads(json.dumps(payload))
    assert serialized["expected_tasks"] == expected_tasks


@pytest.mark.asyncio
async def test_reconstructed_events_include_stable_cursor_and_filter_from_cursor() -> None:
    db = _MemoryDatabase()
    root_id, _ = await _seed_successful_run(db)

    events = await _reconstruct_lifecycle_events(db, root_id)

    assert all(event.cursor == reconstructed_event_cursor(event) for event in events)
    assert _filter_after_cursor(events, None) == events

    filtered = _filter_after_cursor(events, events[2].cursor)
    assert events[3] in filtered
    assert events[-1] in filtered


def test_filter_after_cursor_guarantees_at_least_once_under_clock_skew() -> None:
    now = datetime(2026, 3, 14, 12, 0, tzinfo=UTC)
    raw_events = [
        _ReconstructedEvent(event_type=EventType.RUN_STARTED, span_id="old", timestamp=now - timedelta(seconds=10), data={}, cursor=""),
        _ReconstructedEvent(event_type=EventType.FLOW_STARTED, span_id="near", timestamp=now - timedelta(seconds=3), data={}, cursor=""),
        _ReconstructedEvent(event_type=EventType.TASK_STARTED, span_id="future", timestamp=now + timedelta(seconds=1), data={}, cursor=""),
    ]
    events = [
        _ReconstructedEvent(
            event_type=event.event_type,
            span_id=event.span_id,
            timestamp=event.timestamp,
            data=event.data,
            cursor=reconstructed_event_cursor(event),
        )
        for event in raw_events
    ]
    cursor_event = _ReconstructedEvent(
        event_type=EventType.RUN_STARTED,
        span_id="cursor",
        timestamp=now,
        data={},
        cursor="",
    )

    filtered_once = _filter_after_cursor(events, reconstructed_event_cursor(cursor_event))
    filtered_twice = _filter_after_cursor(events, reconstructed_event_cursor(cursor_event))

    assert [event.span_id for event in filtered_once] == ["near", "future"]
    assert [event.dedup_key for event in filtered_once] == [event.dedup_key for event in filtered_twice]
    assert filtered_once[0].dedup_key == ("near", EventType.FLOW_STARTED.value)
    assert filtered_once[1].dedup_key == ("future", EventType.TASK_STARTED.value)


def test_filter_after_cursor_raises_on_malformed_cursor() -> None:
    event = _ReconstructedEvent(
        event_type=EventType.RUN_STARTED,
        span_id="root",
        timestamp=datetime(2026, 3, 14, 12, 0, tzinfo=UTC),
        data={},
        cursor="",
    )

    with pytest.raises(
        InvalidLifecycleCursorError,
        match=r"Invalid cursor\. Clients should restart from the full history by omitting `after_cursor`\.",
    ):
        _filter_after_cursor([event], "not-a-valid-cursor")


@pytest.mark.asyncio
async def test_reconstruction_maps_process_crash_to_crashed_error_code() -> None:
    db = _MemoryDatabase()
    root_id = uuid4()
    t0 = datetime(2026, 3, 14, 12, 0, tzinfo=UTC)

    deploy = _make_span(
        span_id=root_id,
        deployment_id=root_id,
        root_deployment_id=root_id,
        kind=SpanKind.DEPLOYMENT,
        name="CrashedDeploy",
        status=SpanStatus.FAILED,
        started_at=t0,
        ended_at=t0 + timedelta(seconds=5),
        error_type="ProcessCrashed",
        error_message="process crashed",
        meta_json=json.dumps({"input_fingerprint": "fp", "deployment_class": "CrashPipeline", "flow_plan": [], "error_code": ErrorCode.CRASHED}),
    )
    await db.insert_span(deploy)

    events = await _reconstruct_lifecycle_events(db, root_id)
    run_failed = next(event for event in events if event.event_type == EventType.RUN_FAILED)

    assert run_failed.data["error_code"] == ErrorCode.CRASHED
