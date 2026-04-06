# pyright: reportPrivateUsage=false
"""Tests for task retries, timeouts, cancellation, and retry span detail."""

import asyncio
import json
from types import MappingProxyType
from uuid import uuid7

import pytest

from ai_pipeline_core._llm_core.model_response import ModelResponse
from ai_pipeline_core._llm_core.types import TokenUsage
from ai_pipeline_core.database import SpanKind, SpanStatus
from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.deployment._types import _NoopPublisher
from ai_pipeline_core.documents import Document
from ai_pipeline_core.llm.conversation import Conversation
from ai_pipeline_core.pipeline import PipelineTask, pipeline_test_context
from ai_pipeline_core.pipeline._execution_context import ExecutionContext, FlowFrame, set_execution_context
from ai_pipeline_core.pipeline._runtime_sinks import build_runtime_sinks
from ai_pipeline_core.pipeline.limits import _SharedStatus
from ai_pipeline_core.settings import settings


class EdgeInputDoc(Document):
    """Input for resilience tests."""


class EdgeOutputDoc(Document):
    """Output for resilience tests."""


def _make_response(
    *,
    content: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost: float,
    response_id: str,
) -> ModelResponse[str]:
    return ModelResponse[str](
        content=content,
        parsed=content,
        usage=TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
        cost=cost,
        model="test-model",
        response_id=response_id,
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


class _CancelOnFirstAttempt(PipelineTask):
    retries = 2
    retry_delay_seconds = 0
    attempt_count = 0

    @classmethod
    async def run(cls, input_docs: tuple[EdgeInputDoc, ...]) -> tuple[EdgeOutputDoc, ...]:
        _ = input_docs
        cls.attempt_count += 1
        raise asyncio.CancelledError("externally cancelled")


class _TimeoutWithRetry(PipelineTask):
    retries = 1
    retry_delay_seconds = 0
    timeout_seconds = 1
    attempt_count = 0

    @classmethod
    async def run(cls, input_docs: tuple[EdgeInputDoc, ...]) -> tuple[EdgeOutputDoc, ...]:
        cls.attempt_count += 1
        if cls.attempt_count == 1:
            await asyncio.sleep(10)
        return (EdgeOutputDoc.derive(derived_from=(input_docs[0],), name="out.txt", content="ok"),)


class _InstantTask(PipelineTask):
    @classmethod
    async def run(cls, input_docs: tuple[EdgeInputDoc, ...]) -> tuple[EdgeOutputDoc, ...]:
        return (EdgeOutputDoc.derive(derived_from=(input_docs[0],), name="instant.txt", content="done"),)


class _RetryWithConversationTask(PipelineTask):
    retries = 1
    retry_delay_seconds = 0
    attempt_count = 0

    @classmethod
    async def run(cls, input_docs: tuple[EdgeInputDoc, ...]) -> tuple[EdgeOutputDoc, ...]:
        cls.attempt_count += 1
        conv = Conversation(model="test-model", enable_substitutor=False)
        conv = await conv.send(f"attempt-{cls.attempt_count}", purpose=f"attempt-{cls.attempt_count}")
        if cls.attempt_count == 1:
            raise RuntimeError("retry me")
        return (EdgeOutputDoc.derive(derived_from=(input_docs[0],), name="retried.txt", content=conv.content),)


def _make_input() -> EdgeInputDoc:
    return EdgeInputDoc.create_root(name="in.txt", content="x", reason="task-resilience-test")


def _make_retry_test_context(database: _MemoryDatabase) -> ExecutionContext:
    deployment_id = uuid7()
    flow_span_id = uuid7()
    return ExecutionContext(
        run_id="test-run",
        execution_id=None,
        publisher=_NoopPublisher(),
        limits=MappingProxyType({}),
        limits_status=_SharedStatus(),
        database=database,
        sinks=build_runtime_sinks(database=database, settings_obj=settings),
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        deployment_name="test-pipeline",
        flow_frame=FlowFrame(
            name="test-flow",
            flow_class_name="TestFlow",
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


@pytest.mark.asyncio
async def test_cancelled_error_bypasses_retries() -> None:
    _CancelOnFirstAttempt.attempt_count = 0
    with pipeline_test_context():
        with pytest.raises(asyncio.CancelledError):
            await _CancelOnFirstAttempt.run((_make_input(),))

    assert _CancelOnFirstAttempt.attempt_count == 1


@pytest.mark.asyncio
async def test_timeout_triggers_retry_then_succeeds() -> None:
    _TimeoutWithRetry.attempt_count = 0
    with pipeline_test_context():
        result = await _TimeoutWithRetry.run((_make_input(),))

    assert _TimeoutWithRetry.attempt_count == 2
    assert result[0].name == "out.txt"


@pytest.mark.asyncio
async def test_handle_can_be_awaited_twice() -> None:
    with pipeline_test_context():
        handle = _InstantTask.run((_make_input(),))
        first = await handle
        second = await handle

    assert first[0].sha256 == second[0].sha256


@pytest.mark.asyncio
async def test_handle_concurrent_await_returns_same_result() -> None:
    with pipeline_test_context():
        handle = _InstantTask.run((_make_input(),))
        first, second = await asyncio.gather(handle.result(), handle.result())

    assert first[0].sha256 == second[0].sha256


@pytest.mark.asyncio
async def test_retried_task_persists_final_attempt_and_failed_attempt_llm_rounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ai_pipeline_core.llm.conversation.core_generate",
        _make_fake_generate([
            _make_response(content="first", prompt_tokens=10, completion_tokens=5, cost=0.3, response_id="resp-1"),
            _make_response(content="second", prompt_tokens=20, completion_tokens=6, cost=0.7, response_id="resp-2"),
        ]),
    )
    _RetryWithConversationTask.attempt_count = 0
    database = _MemoryDatabase()
    with set_execution_context(_make_retry_test_context(database)):
        result = await _RetryWithConversationTask.run((_make_input(),))

    assert result[0].name == "retried.txt"
    task_span = next(span for span in database._spans.values() if span.kind == SpanKind.TASK)
    task_meta = json.loads(task_span.meta_json)
    llm_rounds = [span for span in database._spans.values() if span.kind == SpanKind.LLM_ROUND]

    assert task_span.status == SpanStatus.COMPLETED
    assert task_meta["attempt"] == 1
    assert len(llm_rounds) == 2
    assert sorted(span.cost_usd for span in llm_rounds) == [0.3, 0.7]


class _RetryLeakTask(PipelineTask):
    retries = 1
    retry_delay_seconds = 0
    attempt_count = 0

    @classmethod
    async def run(cls, input_docs: tuple[EdgeInputDoc, ...]) -> tuple[EdgeOutputDoc, ...]:
        cls.attempt_count += 1
        result = EdgeOutputDoc.derive(derived_from=input_docs, name=f"attempt-{cls.attempt_count}.txt", content="ok")
        if cls.attempt_count == 1:
            raise RuntimeError("retry once")
        return (result,)


@pytest.mark.asyncio
async def test_retry_discards_created_documents_from_failed_attempts(caplog: pytest.LogCaptureFixture) -> None:
    _RetryLeakTask.attempt_count = 0
    caplog.set_level("WARNING", logger="ai_pipeline_core.pipeline._task")
    with pipeline_test_context():
        result = await _RetryLeakTask.run((_make_input(),))

    assert result[0].name == "attempt-2.txt"
    assert "Orphaned document" not in caplog.text
