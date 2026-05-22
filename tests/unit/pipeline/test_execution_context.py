"""Tests for unified execution context transitions."""

import asyncio
import logging
from types import MappingProxyType
from uuid import uuid7

import pytest

from ai_pipeline_core.deployment._types import _NoopPublisher
from ai_pipeline_core.logger._buffer import ExecutionLogBuffer
from ai_pipeline_core.logger._handler import ExecutionLogHandler
from ai_pipeline_core.logger._logging_config import setup_logging
from ai_pipeline_core.pipeline._execution_context import (
    ExecutionContext,
    FlowFrame,
    TaskFrame,
    get_execution_context,
    get_run_id,
    get_sinks,
    pipeline_test_context,
    record_lifecycle_event,
    set_execution_context,
)
from ai_pipeline_core.pipeline.limits import _SharedStatus


def _make_ctx() -> ExecutionContext:
    return ExecutionContext(
        run_id="test-run",
        execution_id=None,
        publisher=_NoopPublisher(),
        limits=MappingProxyType({}),
        limits_status=_SharedStatus(),
    )


def _make_flow_frame() -> FlowFrame:
    return FlowFrame(
        name="Flow",
        flow_class_name="FlowClass",
        step=1,
        total_steps=2,
        flow_minutes=(1.0, 2.0),
        completed_minutes=0.0,
        flow_params={},
    )


def test_with_flow_returns_new_context_with_flow_frame() -> None:
    ctx = _make_ctx()
    frame = _make_flow_frame()

    updated = ctx.with_flow(frame)

    assert updated is not ctx
    assert updated.flow_frame is frame
    assert updated.task_frame is None
    assert ctx.flow_frame is None


def test_with_task_returns_new_context_with_task_frame() -> None:
    ctx = _make_ctx()
    task_frame = TaskFrame(task_class_name="Task", task_id="task-id", depth=0)

    updated = ctx.with_task(task_frame)

    assert updated.task_frame is task_frame
    assert ctx.task_frame is None


def test_execution_context_has_empty_sink_defaults() -> None:
    ctx = _make_ctx()

    assert ctx.sinks == ()
    assert ctx.service_name == ""


def test_with_span_sets_current_and_parent_span_ids() -> None:
    ctx = _make_ctx()
    parent_span_id = uuid7()
    child_span_id = uuid7()

    updated = ctx.with_span(child_span_id, parent_span_id=parent_span_id)

    assert updated.span_id == child_span_id
    assert updated.parent_span_id == parent_span_id
    assert updated.current_span_id == child_span_id


def test_next_child_sequence_is_tracked_per_parent_span() -> None:
    ctx = _make_ctx()
    first_parent = uuid7()
    second_parent = uuid7()

    assert ctx.next_child_sequence(first_parent) == 0
    assert ctx.next_child_sequence(first_parent) == 1
    assert ctx.next_child_sequence(second_parent) == 0


def test_set_execution_context() -> None:
    ctx = _make_ctx()
    with set_execution_context(ctx):
        assert get_execution_context() is ctx


def test_pipeline_test_context_sets_and_restores() -> None:
    before = get_execution_context()

    with pipeline_test_context(run_id="ctx-test") as ctx:
        assert get_execution_context() is ctx
        assert ctx.run_id == "ctx-test"
        assert ctx.deployment_id is not None
        assert ctx.root_deployment_id == ctx.deployment_id
        assert ctx.span_id == ctx.deployment_id
        assert ctx.current_span_id == ctx.deployment_id

    assert get_execution_context() is before


def test_execution_context_post_init_fills_root_deployment_id_from_deployment_id() -> None:
    deployment_id = uuid7()
    ctx = ExecutionContext(
        run_id="test-run",
        execution_id=None,
        publisher=_NoopPublisher(),
        limits=MappingProxyType({}),
        limits_status=_SharedStatus(),
        deployment_id=deployment_id,
    )

    assert ctx.root_deployment_id == deployment_id


def test_get_run_id_returns_run_id_from_context() -> None:
    with pipeline_test_context(run_id="ctx-test"):
        assert get_run_id() == "ctx-test"


def test_get_run_id_outside_context_raises() -> None:
    with pytest.raises(RuntimeError, match="pipeline_test_context"):
        get_run_id()


def test_record_lifecycle_event_uses_current_span_id_for_log_buffer() -> None:
    setup_logging(level="INFO")
    root_logger = logging.getLogger()
    handler = next((item for item in root_logger.handlers if isinstance(item, ExecutionLogHandler)), None)
    added_handler = False
    if handler is None:
        handler = ExecutionLogHandler()
        root_logger.addHandler(handler)
        added_handler = True

    buffer = ExecutionLogBuffer()
    deployment_id = uuid7()
    current_span_id = uuid7()
    flow_span_id = uuid7()
    ctx = ExecutionContext(
        run_id="test-run",
        execution_id=None,
        publisher=_NoopPublisher(),
        limits=MappingProxyType({}),
        limits_status=_SharedStatus(),
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        current_span_id=current_span_id,
        flow_span_id=flow_span_id,
        log_buffer=buffer,
    )
    try:
        with set_execution_context(ctx):
            record_lifecycle_event("task.started", "Task started", task_name="ExampleTask")
            logs = buffer.drain()
            assert len(logs) == 1
            assert logs[0].span_id == current_span_id
            assert logs[0].category == "lifecycle"
            assert logs[0].event_type == "task.started"
            assert '"task_name": "ExampleTask"' in logs[0].fields_json
    finally:
        if added_handler:
            root_logger.removeHandler(handler)


class _NoopSink:
    async def on_span_started(self, **kwargs: object) -> None:
        _ = kwargs
        await asyncio.sleep(0)

    async def on_span_finished(self, **kwargs: object) -> None:
        _ = kwargs
        await asyncio.sleep(0)


def test_get_sinks_returns_empty_tuple_without_context() -> None:
    assert get_sinks() == ()


def test_get_sinks_reads_current_execution_context() -> None:
    sink = _NoopSink()
    ctx = ExecutionContext(
        run_id="test-run",
        execution_id=None,
        publisher=_NoopPublisher(),
        limits=MappingProxyType({}),
        limits_status=_SharedStatus(),
        sinks=(sink,),
    )
    with set_execution_context(ctx):
        assert get_sinks() == (sink,)
