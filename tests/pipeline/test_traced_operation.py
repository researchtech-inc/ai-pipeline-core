"""Tests for operation span tracking."""

# pyright: reportPrivateUsage=false

import json
from types import MappingProxyType
from uuid import uuid7

import pytest

from ai_pipeline_core import traced_operation
from ai_pipeline_core._llm_core.model_response import ModelResponse
from ai_pipeline_core._llm_core.types import TokenUsage
from ai_pipeline_core.database import SpanKind, SpanStatus
from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.deployment._types import _NoopPublisher
from ai_pipeline_core.documents import Document
from ai_pipeline_core.llm.conversation import Conversation
from ai_pipeline_core.pipeline import PipelineTask
from ai_pipeline_core.pipeline._execution_context import (
    ExecutionContext,
    FlowFrame,
    get_execution_context,
    get_sinks,
    set_execution_context,
)
from ai_pipeline_core.pipeline._runtime_sinks import build_runtime_sinks
from ai_pipeline_core.pipeline.limits import _SharedStatus
from ai_pipeline_core.settings import settings


class _TracedInputDoc(Document):
    """Input document for traced_operation tests."""


class _TracedOutputDoc(Document):
    """Output document for traced_operation tests."""


class _RecordingSpanDatabase(_MemoryDatabase):
    def __init__(self) -> None:
        super().__init__()
        self.inserted_spans: list[object] = []

    async def insert_span(self, span: object) -> None:
        self.inserted_spans.append(span)
        await super().insert_span(span)  # type: ignore[arg-type]


def _make_response(content: str) -> ModelResponse[str]:
    return ModelResponse[str](
        content=content,
        parsed=content,
        usage=TokenUsage(prompt_tokens=10, completion_tokens=4, total_tokens=14),
        cost=0.5,
        model="test-model",
        response_id="resp-test",
        metadata={"time_taken": 0.2, "first_token_time": 0.1},
        tool_calls=(),
    )


def _make_fake_generate(results: list[ModelResponse[str] | BaseException]):
    remaining = list(results)

    async def _fake_generate(*args: object, **kwargs: object) -> ModelResponse[str]:
        _ = (args, kwargs)
        next_item = remaining.pop(0)
        if isinstance(next_item, BaseException):
            raise next_item
        return next_item

    return _fake_generate


def _make_context_with_db(database: _MemoryDatabase) -> ExecutionContext:
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
        flow_frame=FlowFrame(
            name="TraceFlow",
            flow_class_name="TraceFlow",
            step=1,
            total_steps=1,
            flow_minutes=(1.0,),
            completed_minutes=0.0,
            flow_params={},
        ),
        span_id=flow_span_id,
        current_span_id=flow_span_id,
        flow_span_id=flow_span_id,
    )


class _TaskWithTracedOperation(PipelineTask):
    @classmethod
    async def run(cls, input_docs: tuple[_TracedInputDoc, ...]) -> tuple[_TracedOutputDoc, ...]:
        async with traced_operation("inner-span", description="inner description"):
            conv = Conversation(model="test-model", enable_substitutor=False)
            await conv.send("hello", purpose="say-hello")
        return (_TracedOutputDoc.derive(derived_from=(input_docs[0],), name="out.txt", content="ok"),)


def _make_input() -> _TracedInputDoc:
    return _TracedInputDoc.create_root(name="input.txt", content="input", reason="traced-operation-test")


@pytest.mark.asyncio
async def test_traced_operation_creates_completed_operation_span() -> None:
    database = _RecordingSpanDatabase()
    ctx = _make_context_with_db(database)
    with set_execution_context(ctx):
        async with traced_operation("fetch:url", description="source=docs"):
            await _noop()

    operation_span = next(span for span in database._spans.values() if span.kind == SpanKind.OPERATION)
    assert operation_span.name == "fetch:url"
    assert operation_span.description == "source=docs"
    assert operation_span.status == SpanStatus.COMPLETED
    assert operation_span.parent_span_id == ctx.flow_span_id
    assert json.loads(operation_span.meta_json or "{}") == {}


@pytest.mark.asyncio
async def test_traced_operation_marks_failed_span_and_reraises() -> None:
    database = _RecordingSpanDatabase()
    with set_execution_context(_make_context_with_db(database)):
        with pytest.raises(ValueError, match="boom"):
            async with traced_operation("explode"):
                raise ValueError("boom")

    operation_span = next(span for span in database._spans.values() if span.kind == SpanKind.OPERATION)
    assert operation_span.status == SpanStatus.FAILED
    assert operation_span.error_type == "ValueError"
    assert operation_span.error_message == "boom"


@pytest.mark.asyncio
async def test_nested_traced_operations_form_parent_child_hierarchy() -> None:
    database = _RecordingSpanDatabase()
    ctx = _make_context_with_db(database)
    with set_execution_context(ctx):
        async with traced_operation("outer"):
            outer_ctx = get_execution_context()
            outer_sinks = get_sinks()
            async with traced_operation("inner"):
                inner_ctx = get_execution_context()
                inner_sinks = get_sinks()
                assert inner_ctx is not outer_ctx
                assert inner_sinks == outer_sinks
                await _noop()
            assert get_execution_context() is not None
            assert get_sinks() == outer_sinks

    outer_span = next(span for span in database._spans.values() if span.kind == SpanKind.OPERATION and span.name == "outer")
    inner_span = next(span for span in database._spans.values() if span.kind == SpanKind.OPERATION and span.name == "inner")
    assert outer_span.parent_span_id == ctx.flow_span_id
    assert inner_span.parent_span_id == outer_span.span_id


@pytest.mark.asyncio
async def test_traced_operation_inside_task_records_conversation_children(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ai_pipeline_core.llm.conversation.core_generate",
        _make_fake_generate([_make_response("ok")]),
    )
    database = _RecordingSpanDatabase()
    with set_execution_context(_make_context_with_db(database)):
        await _TaskWithTracedOperation.run((_make_input(),))

    task_span = next(span for span in database._spans.values() if span.kind == SpanKind.TASK)
    attempt_span = next(span for span in database._spans.values() if span.kind == SpanKind.ATTEMPT)
    operation_span = next(span for span in database._spans.values() if span.kind == SpanKind.OPERATION)
    conversation_span = next(span for span in database._spans.values() if span.kind == SpanKind.CONVERSATION)
    llm_round_span = next(span for span in database._spans.values() if span.kind == SpanKind.LLM_ROUND)

    assert attempt_span.parent_span_id == task_span.span_id
    assert operation_span.parent_span_id == attempt_span.span_id
    assert conversation_span.parent_span_id == operation_span.span_id
    assert llm_round_span.parent_span_id == conversation_span.span_id
    assert llm_round_span.cost_usd == 0.5
    assert conversation_span.cost_usd == 0.0
    assert operation_span.cost_usd == 0.0


async def _noop() -> None:
    await __import__("asyncio").sleep(0)
