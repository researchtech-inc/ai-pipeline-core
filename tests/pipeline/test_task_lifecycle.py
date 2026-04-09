"""Tests for PipelineTask lifecycle events and append-only span writes."""

import json
from types import MappingProxyType
from uuid import uuid7

import pytest

from ai_pipeline_core.deployment._types import TaskCompletedEvent, TaskFailedEvent, TaskStartedEvent, _MemoryPublisher, _NoopPublisher
from ai_pipeline_core.documents import Document
from ai_pipeline_core.pipeline import PipelineTask, pipeline_test_context
from ai_pipeline_core.pipeline._execution_context import FlowFrame, set_execution_context
from ai_pipeline_core.pipeline._runtime_sinks import build_runtime_sinks
from ai_pipeline_core.pipeline.limits import _SharedStatus
from ai_pipeline_core.database import SpanKind, SpanStatus
from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.pipeline._execution_context import ExecutionContext
from ai_pipeline_core.settings import settings


class _InDoc(Document):
    """Test input document."""


class _OutDoc(Document):
    """Test output document."""


class _PassthroughTask(PipelineTask):
    @classmethod
    async def run(cls, input_docs: tuple[_InDoc, ...]) -> tuple[_OutDoc, ...]:
        _ = input_docs
        return (_OutDoc(name="out.txt", content=b"output"),)


class _FailingTask(PipelineTask):
    retries = 0

    @classmethod
    async def run(cls, input_docs: tuple[_InDoc, ...]) -> tuple[_OutDoc, ...]:
        _ = input_docs
        raise ValueError("task failed deliberately")


class _CacheableTask(PipelineTask):
    cacheable = True
    cache_version = 3
    cache_ttl_seconds = 3600
    run_calls = 0

    @classmethod
    async def run(cls, input_docs: tuple[_InDoc, ...]) -> tuple[_OutDoc, ...]:
        cls.run_calls += 1
        return (_OutDoc.derive(derived_from=(input_docs[0],), name="cached.txt", content=f"call-{cls.run_calls}"),)


class _RecordingSpanDatabase(_MemoryDatabase):
    def __init__(self) -> None:
        super().__init__()
        self.inserted_spans: list[object] = []

    async def insert_span(self, span: object) -> None:
        self.inserted_spans.append(span)
        await super().insert_span(span)  # type: ignore[arg-type]


def _make_flow_frame() -> FlowFrame:
    return FlowFrame(
        name="test-flow",
        flow_class_name="MockFlow",
        step=1,
        total_steps=1,
        flow_minutes=(1.0,),
        completed_minutes=0.0,
        flow_params={},
    )


def _make_input() -> _InDoc:
    return _InDoc(name="input.txt", content=b"test-input")


def _make_span_context(database: _MemoryDatabase) -> ExecutionContext:
    deployment_id = uuid7()
    flow_span_id = uuid7()
    return ExecutionContext(
        run_id="test-run",
        execution_id=None,
        publisher=_NoopPublisher(),
        limits=MappingProxyType({}),
        limits_status=_SharedStatus(),
        database=database,
        sinks=build_runtime_sinks(database=database, settings_obj=settings).span_sinks,
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        deployment_name="test-pipeline",
        flow_frame=_make_flow_frame(),
        span_id=flow_span_id,
        current_span_id=flow_span_id,
        flow_span_id=flow_span_id,
    )


@pytest.mark.asyncio
async def test_task_run_returns_handle() -> None:
    with pipeline_test_context():
        handle = _PassthroughTask.run((_make_input(),))
        result = await handle
        assert len(result) == 1
        assert isinstance(result[0], _OutDoc)


@pytest.mark.asyncio
async def test_task_started_and_completed_events_use_same_span_id() -> None:
    publisher = _MemoryPublisher()
    with pipeline_test_context(publisher=publisher) as ctx:
        with set_execution_context(ctx.with_flow(_make_flow_frame())):
            await _PassthroughTask.run((_make_input(),))

    started = [event for event in publisher.events if isinstance(event, TaskStartedEvent)]
    completed = [event for event in publisher.events if isinstance(event, TaskCompletedEvent)]
    assert len(started) == 1
    assert len(completed) == 1
    assert started[0].task_class == "_PassthroughTask"
    assert completed[0].span_id == started[0].span_id
    assert completed[0].duration_ms >= 0
    assert started[0].root_deployment_id
    assert completed[0].root_deployment_id
    assert started[0].root_deployment_id == completed[0].root_deployment_id


@pytest.mark.asyncio
async def test_task_failed_event_uses_started_span_id() -> None:
    publisher = _MemoryPublisher()
    with pipeline_test_context(publisher=publisher) as ctx:
        with set_execution_context(ctx.with_flow(_make_flow_frame())):
            with pytest.raises(ValueError, match="task failed deliberately"):
                await _FailingTask.run((_make_input(),))

    started = [event for event in publisher.events if isinstance(event, TaskStartedEvent)]
    failed = [event for event in publisher.events if isinstance(event, TaskFailedEvent)]
    assert len(started) == 1
    assert len(failed) == 1
    assert failed[0].span_id == started[0].span_id
    assert "task failed deliberately" in failed[0].error_message
    assert started[0].root_deployment_id
    assert failed[0].root_deployment_id
    assert started[0].root_deployment_id == failed[0].root_deployment_id


@pytest.mark.asyncio
async def test_successful_task_writes_started_then_completed_span_rows() -> None:
    database = _RecordingSpanDatabase()
    with set_execution_context(_make_span_context(database)):
        await _PassthroughTask.run((_make_input(),))

    task_rows = [span for span in database.inserted_spans if getattr(span, "kind", None) == SpanKind.TASK]
    assert len(task_rows) == 2

    started_span, completed_span = task_rows
    assert started_span.status == SpanStatus.RUNNING
    assert completed_span.status == SpanStatus.COMPLETED
    assert started_span.span_id == completed_span.span_id
    assert started_span.ended_at is None
    assert completed_span.ended_at is not None
    assert completed_span.version > started_span.version
    assert json.loads(completed_span.meta_json)["attempt"] == 0


@pytest.mark.asyncio
async def test_failed_task_writes_started_then_failed_span_rows() -> None:
    database = _RecordingSpanDatabase()
    with set_execution_context(_make_span_context(database)):
        with pytest.raises(ValueError, match="task failed deliberately"):
            await _FailingTask.run((_make_input(),))

    task_rows = [span for span in database.inserted_spans if getattr(span, "kind", None) == SpanKind.TASK]
    assert len(task_rows) == 2

    started_span, failed_span = task_rows
    assert started_span.status == SpanStatus.RUNNING
    assert failed_span.status == SpanStatus.FAILED
    assert failed_span.error_type == "ValueError"
    assert failed_span.error_message == "task failed deliberately"
    assert json.loads(failed_span.input_json)["input_docs"]["items"][0]["sha256"] == _make_input().sha256


@pytest.mark.asyncio
async def test_completed_cacheable_task_persists_task_cache_key() -> None:
    _CacheableTask.run_calls = 0
    database = _RecordingSpanDatabase()
    with set_execution_context(_make_span_context(database)):
        await _CacheableTask.run((_make_input(),))

    task_rows = [span for span in database.inserted_spans if getattr(span, "kind", None) == SpanKind.TASK]
    completed_span = task_rows[-1]
    assert completed_span.status == SpanStatus.COMPLETED
    assert completed_span.cache_key.startswith(f"task:{_CacheableTask.__module__}:{_CacheableTask.__qualname__}:v3:")


@pytest.mark.asyncio
async def test_second_identical_cacheable_task_run_reuses_cached_outputs() -> None:
    _CacheableTask.run_calls = 0
    database = _RecordingSpanDatabase()
    with set_execution_context(_make_span_context(database)):
        first_result = await _CacheableTask.run((_make_input(),))

    with set_execution_context(_make_span_context(database)):
        second_result = await _CacheableTask.run((_make_input(),))

    assert _CacheableTask.run_calls == 1
    assert second_result[0].sha256 == first_result[0].sha256
    task_rows = [span for span in database.inserted_spans if getattr(span, "kind", None) == SpanKind.TASK]
    cached_span = task_rows[-1]
    assert cached_span.status == SpanStatus.CACHED
    assert cached_span.output_document_shas == (first_result[0].sha256,)
