"""Tests for orphaned span recovery."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from ai_pipeline_core.database import SpanKind, SpanRecord, SpanStatus
from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.deployment._event_reconstruction import _reconstruct_lifecycle_events
from ai_pipeline_core.deployment._types import ErrorCode, EventType
from ai_pipeline_core.observability._recovery import recover_orphaned_spans


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
        "status": SpanStatus.RUNNING,
        "sequence_no": 0,
        "started_at": started_at,
        "ended_at": kwargs.pop("ended_at", None),
        "version": 1,
        "meta_json": "",
        "metrics_json": "",
    }
    defaults.update(kwargs)
    return SpanRecord(**defaults)


@pytest.mark.asyncio
async def test_recover_orphaned_spans_marks_tree_failed_and_reconstructs_crash_code() -> None:
    db = _MemoryDatabase()
    root_id = uuid4()
    flow_id = uuid4()
    task_id = uuid4()
    started_at = datetime.now(UTC) - timedelta(hours=3)

    deploy = _make_span(
        span_id=root_id,
        deployment_id=root_id,
        root_deployment_id=root_id,
        kind=SpanKind.DEPLOYMENT,
        name="Deploy",
        started_at=started_at,
        meta_json=json.dumps({"deployment_class": "TestDeployment", "flow_plan": []}),
    )
    flow = _make_span(
        span_id=flow_id,
        parent_span_id=root_id,
        deployment_id=root_id,
        root_deployment_id=root_id,
        kind=SpanKind.FLOW,
        name="Flow",
        started_at=started_at + timedelta(seconds=5),
        meta_json=json.dumps({"step": 1, "total_steps": 1, "expected_tasks": []}),
    )
    task = _make_span(
        span_id=task_id,
        parent_span_id=flow_id,
        deployment_id=root_id,
        root_deployment_id=root_id,
        kind=SpanKind.TASK,
        name="Task",
        started_at=started_at + timedelta(seconds=10),
    )
    for span in (deploy, flow, task):
        await db.insert_span(span)

    recovered = await recover_orphaned_spans(db, older_than=datetime.now(UTC) - timedelta(minutes=1))

    assert recovered == ["test-run"]
    root_span = await db.get_span(root_id)
    assert root_span is not None
    assert root_span.status == SpanStatus.FAILED
    assert root_span.error_type == "ProcessCrashed"
    assert json.loads(root_span.meta_json)["error_code"] == ErrorCode.CRASHED

    events = await _reconstruct_lifecycle_events(db, root_id)
    run_failed = next(event for event in events if event.event_type == EventType.RUN_FAILED)
    assert run_failed.data["error_code"] == ErrorCode.CRASHED
