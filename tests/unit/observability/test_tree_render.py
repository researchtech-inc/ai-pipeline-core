"""Tests for span tree rendering and tree view construction."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from ai_pipeline_core.database import SpanKind, SpanRecord, SpanStatus
from ai_pipeline_core.database.snapshot._spans import build_span_tree_view
from ai_pipeline_core.observability._tree_render import format_span_tree_lines


def _make_span(**kwargs: object) -> SpanRecord:
    deployment_id = kwargs.pop("deployment_id", uuid4())
    root_deployment_id = kwargs.pop("root_deployment_id", deployment_id)
    started_at = kwargs.pop("started_at", datetime(2026, 3, 11, 12, 0, tzinfo=UTC))
    if not isinstance(started_at, datetime):
        msg = "started_at must be a datetime"
        raise TypeError(msg)
    defaults: dict[str, object] = {
        "span_id": kwargs.pop("span_id", uuid4()),
        "parent_span_id": None,
        "deployment_id": deployment_id,
        "root_deployment_id": root_deployment_id,
        "run_id": "span-run",
        "deployment_name": "span-pipeline",
        "kind": SpanKind.TASK,
        "name": "ExampleTask",
        "description": "",
        "status": SpanStatus.COMPLETED,
        "sequence_no": 0,
        "started_at": started_at,
        "ended_at": started_at + timedelta(seconds=1),
        "version": 1,
        "cache_key": "",
        "previous_conversation_id": None,
        "cost_usd": 0.0,
        "error_type": "",
        "error_message": "",
        "input_document_shas": (),
        "output_document_shas": (),
        "target": "",
        "receiver_json": "",
        "input_json": "",
        "output_json": "",
        "error_json": "",
        "meta_json": "",
        "metrics_json": "",
        "input_blob_shas": (),
        "output_blob_shas": (),
    }
    defaults.update(kwargs)
    return SpanRecord(**defaults)


def test_format_span_tree_lines_reports_cycle_once() -> None:
    deployment_id = uuid4()
    base = datetime(2026, 3, 11, 12, 0, tzinfo=UTC)
    deployment = _make_span(
        span_id=uuid4(),
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        kind=SpanKind.DEPLOYMENT,
        name="RootDeployment",
        deployment_name="span-pipeline",
        started_at=base,
        ended_at=base + timedelta(seconds=10),
    )
    first = _make_span(
        span_id=uuid4(),
        parent_span_id=deployment.span_id,
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        kind=SpanKind.TASK,
        name="FirstTask",
        sequence_no=1,
        started_at=base + timedelta(seconds=1),
        ended_at=base + timedelta(seconds=4),
    )
    second = _make_span(
        span_id=uuid4(),
        parent_span_id=first.span_id,
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        kind=SpanKind.TASK,
        name="SecondTask",
        sequence_no=2,
        started_at=base + timedelta(seconds=4),
        ended_at=base + timedelta(seconds=7),
    )

    view = build_span_tree_view([deployment, first, second], deployment_id)
    assert view is not None
    view.children_map[second.span_id] = [first.span_id]

    lines = format_span_tree_lines(view)

    assert any("cycle detected" in line for line in lines)


def test_format_span_tree_lines_include_snapshot_filenames() -> None:
    deployment_id = uuid4()
    base = datetime(2026, 3, 11, 12, 0, tzinfo=UTC)
    deployment = _make_span(
        span_id=uuid4(),
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        kind=SpanKind.DEPLOYMENT,
        name="RootDeployment",
        deployment_name="span-pipeline",
        started_at=base,
        ended_at=base + timedelta(seconds=10),
    )
    flow = _make_span(
        span_id=uuid4(),
        parent_span_id=deployment.span_id,
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        kind=SpanKind.FLOW,
        name="GatherFlow",
        sequence_no=1,
        started_at=base + timedelta(seconds=1),
        ended_at=base + timedelta(seconds=4),
    )
    task = _make_span(
        span_id=uuid4(),
        parent_span_id=flow.span_id,
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        kind=SpanKind.TASK,
        name="GatherTask",
        sequence_no=1,
        started_at=base + timedelta(seconds=2),
        ended_at=base + timedelta(seconds=3),
    )
    conversation = _make_span(
        span_id=uuid4(),
        parent_span_id=task.span_id,
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        kind=SpanKind.CONVERSATION,
        name="ConversationBoundary",
        sequence_no=1,
        started_at=base + timedelta(seconds=3),
        ended_at=base + timedelta(seconds=4),
        meta_json=json.dumps({"model": "gpt-5.4-mini", "purpose": "analyze_document"}),
    )
    llm_round = _make_span(
        span_id=uuid4(),
        parent_span_id=conversation.span_id,
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        kind=SpanKind.LLM_ROUND,
        name="round-1",
        sequence_no=1,
        started_at=base + timedelta(seconds=4),
        ended_at=base + timedelta(seconds=5),
        meta_json=json.dumps({"model": "gpt-5.4-mini", "round_index": 1}),
    )

    view = build_span_tree_view([deployment, flow, task, conversation, llm_round], deployment_id)
    assert view is not None

    lines = format_span_tree_lines(view, include_filenames=True)

    assert any("flow-" in line and ".json" in line for line in lines)
    assert any("task-" in line and ".json" in line for line in lines)
    assert any("conv-" in line and ".json" in line for line in lines)
    assert not any("llm_round" in line and ".json" in line for line in lines)
