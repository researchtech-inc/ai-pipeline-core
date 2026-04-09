# pyright: reportPrivateUsage=false
"""Tests for retry observability: ATTEMPT spans, retry error recording, tree rendering, event reconstruction, and replay exclusion."""

import json
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from uuid import UUID, uuid4, uuid7

import pytest

from ai_pipeline_core._llm_core.model_response import ModelResponse
from ai_pipeline_core._llm_core.types import TokenUsage
from ai_pipeline_core.database import SpanKind, SpanRecord, SpanStatus
from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.deployment._event_reconstruction import _reconstruct_lifecycle_events
from ai_pipeline_core.deployment._types import _NoopPublisher
from ai_pipeline_core.documents import Document
from ai_pipeline_core.llm.conversation import Conversation
from ai_pipeline_core.pipeline import PipelineFlow, PipelineTask
from ai_pipeline_core.pipeline._execution_context import ExecutionContext, FlowFrame, set_execution_context
from ai_pipeline_core.pipeline.options import FlowOptions
from ai_pipeline_core.pipeline._runtime_sinks import build_runtime_sinks
from ai_pipeline_core.pipeline._span_types import SpanContext
from ai_pipeline_core.pipeline.limits import _SharedStatus
from ai_pipeline_core.replay import find_experiment_span_ids
from ai_pipeline_core.settings import settings


# ---------------------------------------------------------------------------
# Document types
# ---------------------------------------------------------------------------


class _RetryInputDoc(Document):
    """Input for retry observability tests."""


class _RetryOutputDoc(Document):
    """Output for retry observability tests."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_input() -> _RetryInputDoc:
    return _RetryInputDoc.create_root(name="in.txt", content="x", reason="retry-test")


def _make_context(database: _MemoryDatabase) -> ExecutionContext:
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
        deployment_name="retry-test-pipeline",
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


def _make_response(content: str, cost: float = 0.1) -> ModelResponse[str]:
    return ModelResponse[str](
        content=content,
        parsed=content,
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        cost=cost,
        model="test-model",
        response_id=f"resp-{uuid4().hex[:8]}",
        metadata={"time_taken": 0.1, "first_token_time": 0.05},
        tool_calls=(),
    )


def _make_fake_generate(results: list[ModelResponse[str] | BaseException]):
    remaining = list(results)

    async def _fake_generate(*args: object, **kwargs: object) -> ModelResponse[str]:
        _ = (args, kwargs)
        item = remaining.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    return _fake_generate


def _make_span_record(**kwargs: object) -> SpanRecord:
    deployment_id = kwargs.pop("deployment_id", uuid4())
    root_deployment_id = kwargs.pop("root_deployment_id", deployment_id)
    started_at_raw = kwargs.pop("started_at", datetime(2026, 3, 17, 12, 0, tzinfo=UTC))
    started_at = started_at_raw if isinstance(started_at_raw, datetime) else datetime(2026, 3, 17, 12, 0, tzinfo=UTC)
    defaults: dict[str, object] = {
        "span_id": kwargs.pop("span_id", uuid4()),
        "parent_span_id": None,
        "deployment_id": deployment_id,
        "root_deployment_id": root_deployment_id,
        "run_id": "test-run",
        "deployment_name": "retry-test",
        "kind": SpanKind.TASK,
        "name": "TestSpan",
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


# ---------------------------------------------------------------------------
# Task definitions
# ---------------------------------------------------------------------------


class _FailOnceTask(PipelineTask):
    retries = 1
    retry_delay_seconds = 0
    attempt_count = 0

    @classmethod
    async def run(cls, input_docs: tuple[_RetryInputDoc, ...]) -> tuple[_RetryOutputDoc, ...]:
        cls.attempt_count += 1
        if cls.attempt_count == 1:
            raise RuntimeError("transient failure")
        return (_RetryOutputDoc.derive(derived_from=(input_docs[0],), name="out.txt", content="ok"),)


class _AlwaysFailTask(PipelineTask):
    retries = 2
    retry_delay_seconds = 0

    @classmethod
    async def run(cls, input_docs: tuple[_RetryInputDoc, ...]) -> tuple[_RetryOutputDoc, ...]:
        _ = input_docs
        raise RuntimeError("always fails")


class _NoRetryTask(PipelineTask):
    retries = 0

    @classmethod
    async def run(cls, input_docs: tuple[_RetryInputDoc, ...]) -> tuple[_RetryOutputDoc, ...]:
        return (_RetryOutputDoc.derive(derived_from=(input_docs[0],), name="out.txt", content="ok"),)


class _NonRetriableFailTask(PipelineTask):
    retries = 2
    retry_delay_seconds = 0

    @classmethod
    async def run(cls, input_docs: tuple[_RetryInputDoc, ...]) -> tuple[_RetryOutputDoc, ...]:
        _ = input_docs
        from ai_pipeline_core._base_exceptions import NonRetriableError

        raise NonRetriableError("non-retriable")


class _FailOnceFlow(PipelineFlow):
    _attempt_count = 0

    async def run(self, input_docs: tuple[_RetryInputDoc, ...], options: FlowOptions) -> tuple[_RetryOutputDoc, ...]:
        _ = options
        _FailOnceFlow._attempt_count += 1
        if _FailOnceFlow._attempt_count == 1:
            raise RuntimeError("flow transient failure")
        return tuple(_RetryOutputDoc.derive(derived_from=(doc,), name=f"out-{i}.txt", content="ok") for i, doc in enumerate(input_docs))


class _AlwaysFailFlow(PipelineFlow):
    async def run(self, input_docs: tuple[_RetryInputDoc, ...], options: FlowOptions) -> tuple[_RetryOutputDoc, ...]:
        _ = (input_docs, options)
        raise RuntimeError("flow always fails")


class _NoRetryFailTask(PipelineTask):
    retries = 0

    @classmethod
    async def run(cls, input_docs: tuple[_RetryInputDoc, ...]) -> tuple[_RetryOutputDoc, ...]:
        _ = input_docs
        raise RuntimeError("no-retry failure")


class _NoRetryFlow(PipelineFlow):
    async def run(self, input_docs: tuple[_RetryInputDoc, ...], options: FlowOptions) -> tuple[_RetryOutputDoc, ...]:
        _ = options
        return tuple(_RetryOutputDoc.derive(derived_from=(doc,), name=f"out-{i}.txt", content="ok") for i, doc in enumerate(input_docs))


class _NoRetryConvTask(PipelineTask):
    retries = 0

    @classmethod
    async def run(cls, input_docs: tuple[_RetryInputDoc, ...]) -> tuple[_RetryOutputDoc, ...]:
        conv = Conversation(model="test-model", enable_substitutor=False)
        conv = await conv.send("hello", purpose="test")
        return (_RetryOutputDoc.derive(derived_from=(input_docs[0],), name="out.txt", content=conv.content),)


class _RetryWithConversationTask(PipelineTask):
    retries = 1
    retry_delay_seconds = 0
    attempt_count = 0

    @classmethod
    async def run(cls, input_docs: tuple[_RetryInputDoc, ...]) -> tuple[_RetryOutputDoc, ...]:
        cls.attempt_count += 1
        conv = Conversation(model="test-model", enable_substitutor=False)
        conv = await conv.send(f"attempt-{cls.attempt_count}", purpose=f"attempt-{cls.attempt_count}")
        if cls.attempt_count == 1:
            raise RuntimeError("retry me")
        return (_RetryOutputDoc.derive(derived_from=(input_docs[0],), name="out.txt", content=conv.content),)


# ===========================================================================
# SpanContext unit tests
# ===========================================================================


class TestSpanContextRetryRecording:
    def test_record_retry_failure_stores_structured_error(self) -> None:
        ctx = SpanContext(span_id=uuid7(), parent_span_id=None)
        exc = RuntimeError("boom")
        ctx.record_retry_failure(
            exc=exc,
            attempt=0,
            max_attempts=3,
            attempt_span_id="span-abc",
            will_retry=True,
            delay_seconds=2.0,
        )

        assert len(ctx._retry_errors) == 1
        entry = ctx._retry_errors[0]
        assert entry["attempt"] == 0
        assert entry["max_attempts"] == 3
        assert entry["attempt_span_id"] == "span-abc"
        assert entry["will_retry"] is True
        assert entry["delay_seconds"] == 2.0
        assert entry["error_type"] == "RuntimeError"
        assert entry["error_message"] == "boom"
        assert "RuntimeError: boom" in entry["traceback"]

    def test_retry_errors_empty_by_default(self) -> None:
        ctx = SpanContext(span_id=uuid7(), parent_span_id=None)
        assert ctx._retry_errors == []

    def test_multiple_retry_failures_accumulate_in_order(self) -> None:
        ctx = SpanContext(span_id=uuid7(), parent_span_id=None)
        for i in range(3):
            ctx.record_retry_failure(
                exc=RuntimeError(f"fail-{i}"),
                attempt=i,
                max_attempts=3,
                will_retry=i < 2,
            )

        assert len(ctx._retry_errors) == 3
        assert [e["attempt"] for e in ctx._retry_errors] == [0, 1, 2]
        assert ctx._retry_errors[0]["will_retry"] is True
        assert ctx._retry_errors[2]["will_retry"] is False

    def test_leaf_retry_has_empty_attempt_span_id(self) -> None:
        ctx = SpanContext(span_id=uuid7(), parent_span_id=None)
        ctx.record_retry_failure(exc=TimeoutError("timeout"), attempt=0, max_attempts=2, will_retry=True)
        assert ctx._retry_errors[0]["attempt_span_id"] == ""

    def test_traceback_captures_original_raise_site(self) -> None:
        ctx = SpanContext(span_id=uuid7(), parent_span_id=None)
        try:
            raise ValueError("from here")
        except ValueError as exc:
            ctx.record_retry_failure(exc=exc, attempt=0, max_attempts=1, will_retry=False)

        assert "raise ValueError" in ctx._retry_errors[0]["traceback"]


# ===========================================================================
# Task retry ATTEMPT span tests
# ===========================================================================


class TestTaskRetryAttemptSpans:
    @pytest.mark.asyncio
    async def test_task_retry_creates_attempt_spans(self) -> None:
        _FailOnceTask.attempt_count = 0
        database = _MemoryDatabase()
        with set_execution_context(_make_context(database)):
            await _FailOnceTask.run((_make_input(),))

        task_span = next(s for s in database._spans.values() if s.kind == SpanKind.TASK)
        attempt_spans = sorted(
            (s for s in database._spans.values() if s.kind == SpanKind.ATTEMPT),
            key=lambda s: s.sequence_no,
        )

        assert len(attempt_spans) == 2
        assert all(s.parent_span_id == task_span.span_id for s in attempt_spans)
        assert attempt_spans[0].status == SpanStatus.FAILED
        assert attempt_spans[1].status == SpanStatus.COMPLETED

        task_meta = json.loads(task_span.meta_json)
        assert task_meta["retry_count"] == 1
        assert len(task_meta["retry_errors"]) == 1
        assert task_meta["retry_errors"][0]["attempt_span_id"] == str(attempt_spans[0].span_id)

    @pytest.mark.asyncio
    async def test_single_attempt_span_even_without_retries(self) -> None:
        """Tasks with retries=0 still emit one ATTEMPT span for uniform tree structure."""
        database = _MemoryDatabase()
        with set_execution_context(_make_context(database)):
            await _NoRetryTask.run((_make_input(),))

        attempt_spans = [s for s in database._spans.values() if s.kind == SpanKind.ATTEMPT]
        assert len(attempt_spans) == 1
        assert attempt_spans[0].status == SpanStatus.COMPLETED
        assert json.loads(attempt_spans[0].meta_json) == {"attempt": 0, "max_attempts": 1}

        task_span = next(s for s in database._spans.values() if s.kind == SpanKind.TASK)
        task_meta = json.loads(task_span.meta_json)
        assert "retry_count" not in task_meta
        assert "retry_errors" not in task_meta

    @pytest.mark.asyncio
    async def test_no_retry_task_failure_creates_failed_attempt_span(self) -> None:
        """A retries=0 task that fails emits a failed ATTEMPT span but no retry_errors on the TASK span."""
        database = _MemoryDatabase()
        with set_execution_context(_make_context(database)):
            with pytest.raises(RuntimeError, match="no-retry failure"):
                await _NoRetryFailTask.run((_make_input(),))

        attempt_spans = [s for s in database._spans.values() if s.kind == SpanKind.ATTEMPT]
        assert len(attempt_spans) == 1
        assert attempt_spans[0].status == SpanStatus.FAILED
        assert attempt_spans[0].error_json
        error_data = json.loads(attempt_spans[0].error_json)
        assert error_data["type_name"] == "RuntimeError"
        assert "no-retry failure" in error_data["message"]

        task_span = next(s for s in database._spans.values() if s.kind == SpanKind.TASK)
        task_meta = json.loads(task_span.meta_json)
        assert "retry_count" not in task_meta
        assert "retry_errors" not in task_meta

    @pytest.mark.asyncio
    async def test_all_attempts_fail_records_all_errors(self) -> None:
        database = _MemoryDatabase()
        with set_execution_context(_make_context(database)):
            with pytest.raises(RuntimeError, match="always fails"):
                await _AlwaysFailTask.run((_make_input(),))

        task_span = next(s for s in database._spans.values() if s.kind == SpanKind.TASK)
        attempt_spans = [s for s in database._spans.values() if s.kind == SpanKind.ATTEMPT]

        assert task_span.status == SpanStatus.FAILED
        assert len(attempt_spans) == 3
        assert all(s.status == SpanStatus.FAILED for s in attempt_spans)

        task_meta = json.loads(task_span.meta_json)
        assert task_meta["retry_count"] == 3
        errors = task_meta["retry_errors"]
        assert len(errors) == 3
        assert errors[-1]["will_retry"] is False

    @pytest.mark.asyncio
    async def test_non_retriable_error_propagates_immediately(self) -> None:
        from ai_pipeline_core._base_exceptions import NonRetriableError

        database = _MemoryDatabase()
        with set_execution_context(_make_context(database)):
            with pytest.raises(NonRetriableError):
                await _NonRetriableFailTask.run((_make_input(),))

        attempt_spans = [s for s in database._spans.values() if s.kind == SpanKind.ATTEMPT]
        assert len(attempt_spans) == 1
        assert attempt_spans[0].status == SpanStatus.FAILED

    @pytest.mark.asyncio
    async def test_retry_children_parent_to_attempt_span(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "ai_pipeline_core.llm.conversation.core_generate",
            _make_fake_generate([_make_response("first"), _make_response("second")]),
        )
        _RetryWithConversationTask.attempt_count = 0
        database = _MemoryDatabase()
        with set_execution_context(_make_context(database)):
            await _RetryWithConversationTask.run((_make_input(),))

        attempt_spans = sorted(
            (s for s in database._spans.values() if s.kind == SpanKind.ATTEMPT),
            key=lambda s: s.sequence_no,
        )
        conversation_spans = sorted(
            (s for s in database._spans.values() if s.kind == SpanKind.CONVERSATION),
            key=lambda s: s.sequence_no,
        )

        assert len(attempt_spans) == 2
        assert len(conversation_spans) == 2
        assert conversation_spans[0].parent_span_id == attempt_spans[0].span_id
        assert conversation_spans[1].parent_span_id == attempt_spans[1].span_id

    @pytest.mark.asyncio
    async def test_no_retry_task_children_parent_to_attempt_span(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With retries=0, children (CONVERSATION etc.) must parent to the ATTEMPT span, not directly to TASK."""
        monkeypatch.setattr(
            "ai_pipeline_core.llm.conversation.core_generate",
            _make_fake_generate([_make_response("result")]),
        )

        database = _MemoryDatabase()
        with set_execution_context(_make_context(database)):
            await _NoRetryConvTask.run((_make_input(),))

        attempt_spans = [s for s in database._spans.values() if s.kind == SpanKind.ATTEMPT]
        conversation_spans = [s for s in database._spans.values() if s.kind == SpanKind.CONVERSATION]
        assert len(attempt_spans) == 1
        assert len(conversation_spans) == 1
        assert conversation_spans[0].parent_span_id == attempt_spans[0].span_id

    @pytest.mark.asyncio
    async def test_exception_chain_not_self_referential_after_retry(self) -> None:
        """Regression: raise _attach_task_attempt(exc, attempt) from exc used to set exc.__cause__ = exc."""
        database = _MemoryDatabase()
        with set_execution_context(_make_context(database)):
            with pytest.raises(RuntimeError) as exc_info:
                await _AlwaysFailTask.run((_make_input(),))
        assert exc_info.value.__cause__ is not exc_info.value

    @pytest.mark.asyncio
    async def test_failed_attempt_span_has_error_json(self) -> None:
        """Failed ATTEMPT spans must have error_json populated with the exception details."""
        database = _MemoryDatabase()
        with set_execution_context(_make_context(database)):
            with pytest.raises(RuntimeError):
                await _AlwaysFailTask.run((_make_input(),))

        failed_attempts = [s for s in database._spans.values() if s.kind == SpanKind.ATTEMPT and s.status == SpanStatus.FAILED]
        assert len(failed_attempts) == 3
        for span in failed_attempts:
            assert span.error_json, f"ATTEMPT span {span.span_id} has empty error_json"
            error_data = json.loads(span.error_json)
            assert error_data["type_name"] == "RuntimeError"
            assert "always fails" in error_data["message"]

    @pytest.mark.asyncio
    async def test_task_retry_lifecycle_event_includes_error_message(self) -> None:
        _FailOnceTask.attempt_count = 0
        database = _MemoryDatabase()
        with set_execution_context(_make_context(database)):
            await _FailOnceTask.run((_make_input(),))

        task_span = next(s for s in database._spans.values() if s.kind == SpanKind.TASK)
        task_meta = json.loads(task_span.meta_json)
        assert task_meta["retry_errors"][0]["error_message"] == "transient failure"


# ===========================================================================
# Snapshot rendering tests
# ===========================================================================


class TestSnapshotRendering:
    def _build_retry_tree(self) -> tuple[list[SpanRecord], UUID]:
        """Build a span tree with ATTEMPT spans for rendering tests."""
        deployment_id = uuid4()
        t0 = datetime(2026, 3, 17, 12, 0, tzinfo=UTC)

        deployment = _make_span_record(
            deployment_id=deployment_id,
            root_deployment_id=deployment_id,
            kind=SpanKind.DEPLOYMENT,
            name="TestDeploy",
            started_at=t0,
            ended_at=t0 + timedelta(seconds=30),
        )
        flow = _make_span_record(
            parent_span_id=deployment.span_id,
            deployment_id=deployment_id,
            root_deployment_id=deployment_id,
            kind=SpanKind.FLOW,
            name="RetryFlow",
            sequence_no=1,
            started_at=t0 + timedelta(seconds=1),
            ended_at=t0 + timedelta(seconds=25),
            meta_json=json.dumps({"step": 1, "total_steps": 1}),
        )
        task = _make_span_record(
            parent_span_id=flow.span_id,
            deployment_id=deployment_id,
            root_deployment_id=deployment_id,
            kind=SpanKind.TASK,
            name="RetryTask",
            sequence_no=1,
            started_at=t0 + timedelta(seconds=2),
            ended_at=t0 + timedelta(seconds=20),
            meta_json=json.dumps({"retry_count": 1, "retry_errors": [{"attempt": 0, "will_retry": True}]}),
        )
        attempt_0 = _make_span_record(
            parent_span_id=task.span_id,
            deployment_id=deployment_id,
            root_deployment_id=deployment_id,
            kind=SpanKind.ATTEMPT,
            name="attempt-0",
            sequence_no=1,
            status=SpanStatus.FAILED,
            started_at=t0 + timedelta(seconds=2),
            ended_at=t0 + timedelta(seconds=8),
            meta_json=json.dumps({"attempt": 0, "max_attempts": 2}),
            error_type="RuntimeError",
            error_message="transient",
        )
        attempt_1 = _make_span_record(
            parent_span_id=task.span_id,
            deployment_id=deployment_id,
            root_deployment_id=deployment_id,
            kind=SpanKind.ATTEMPT,
            name="attempt-1",
            sequence_no=2,
            started_at=t0 + timedelta(seconds=9),
            ended_at=t0 + timedelta(seconds=20),
            meta_json=json.dumps({"attempt": 1, "max_attempts": 2}),
        )
        op_failed = _make_span_record(
            parent_span_id=attempt_0.span_id,
            deployment_id=deployment_id,
            root_deployment_id=deployment_id,
            kind=SpanKind.OPERATION,
            name="do_work",
            sequence_no=1,
            status=SpanStatus.FAILED,
            started_at=t0 + timedelta(seconds=3),
            ended_at=t0 + timedelta(seconds=7),
            error_type="RuntimeError",
            error_message="transient",
        )
        op_ok = _make_span_record(
            parent_span_id=attempt_1.span_id,
            deployment_id=deployment_id,
            root_deployment_id=deployment_id,
            kind=SpanKind.OPERATION,
            name="do_work",
            sequence_no=2,
            started_at=t0 + timedelta(seconds=10),
            ended_at=t0 + timedelta(seconds=19),
        )
        return [deployment, flow, task, attempt_0, attempt_1, op_failed, op_ok], deployment_id

    def test_attempt_spans_render_in_tree(self) -> None:
        from ai_pipeline_core.database.snapshot._spans import build_span_tree_view
        from ai_pipeline_core.observability._tree_render import format_span_tree_lines

        tree, deployment_id = self._build_retry_tree()
        view = build_span_tree_view(tree, deployment_id)
        assert view is not None

        lines = format_span_tree_lines(view)
        text = "\n".join(lines)

        assert "attempt: 0/2 failed" in text
        assert "attempt: 1/2 completed" in text
        assert "do_work" in text

    def test_single_attempt_collapsed_in_tree(self) -> None:
        from ai_pipeline_core.database.snapshot._spans import build_span_tree_view
        from ai_pipeline_core.observability._tree_render import format_span_tree_lines

        deployment_id = uuid4()
        t0 = datetime(2026, 3, 17, 12, 0, tzinfo=UTC)
        deployment = _make_span_record(
            deployment_id=deployment_id,
            root_deployment_id=deployment_id,
            kind=SpanKind.DEPLOYMENT,
            name="Deploy",
            started_at=t0,
            ended_at=t0 + timedelta(seconds=5),
        )
        task = _make_span_record(
            parent_span_id=deployment.span_id,
            deployment_id=deployment_id,
            root_deployment_id=deployment_id,
            kind=SpanKind.TASK,
            name="SingleTask",
            sequence_no=1,
            started_at=t0 + timedelta(seconds=1),
            ended_at=t0 + timedelta(seconds=4),
        )
        attempt = _make_span_record(
            parent_span_id=task.span_id,
            deployment_id=deployment_id,
            root_deployment_id=deployment_id,
            kind=SpanKind.ATTEMPT,
            name="attempt-0",
            sequence_no=1,
            started_at=t0 + timedelta(seconds=1),
            ended_at=t0 + timedelta(seconds=4),
            meta_json=json.dumps({"attempt": 0, "max_attempts": 1}),
        )
        operation = _make_span_record(
            parent_span_id=attempt.span_id,
            deployment_id=deployment_id,
            root_deployment_id=deployment_id,
            kind=SpanKind.OPERATION,
            name="inner_op",
            sequence_no=1,
            started_at=t0 + timedelta(seconds=2),
            ended_at=t0 + timedelta(seconds=3),
        )

        view = build_span_tree_view([deployment, task, attempt, operation], deployment_id)
        assert view is not None

        lines = format_span_tree_lines(view)
        text = "\n".join(lines)

        assert "attempt:" not in text
        assert "inner_op" in text

    def test_failed_single_attempt_shown_in_tree(self) -> None:
        """A failed single ATTEMPT must not be collapsed — its timing and error must be visible."""
        from ai_pipeline_core.database.snapshot._spans import build_span_tree_view
        from ai_pipeline_core.observability._tree_render import format_span_tree_lines

        deployment_id = uuid4()
        t0 = datetime(2026, 3, 17, 12, 0, tzinfo=UTC)
        deployment = _make_span_record(
            deployment_id=deployment_id,
            root_deployment_id=deployment_id,
            kind=SpanKind.DEPLOYMENT,
            name="Deploy",
            started_at=t0,
            ended_at=t0 + timedelta(seconds=5),
        )
        task = _make_span_record(
            parent_span_id=deployment.span_id,
            deployment_id=deployment_id,
            root_deployment_id=deployment_id,
            kind=SpanKind.TASK,
            name="FailTask",
            sequence_no=1,
            status=SpanStatus.FAILED,
            started_at=t0 + timedelta(seconds=1),
            ended_at=t0 + timedelta(seconds=4),
        )
        attempt = _make_span_record(
            parent_span_id=task.span_id,
            deployment_id=deployment_id,
            root_deployment_id=deployment_id,
            kind=SpanKind.ATTEMPT,
            name="attempt-0",
            sequence_no=1,
            status=SpanStatus.FAILED,
            started_at=t0 + timedelta(seconds=1),
            ended_at=t0 + timedelta(seconds=4),
            meta_json=json.dumps({"attempt": 0, "max_attempts": 1}),
            error_type="RuntimeError",
            error_message="no-retry failure",
        )

        view = build_span_tree_view([deployment, task, attempt], deployment_id)
        assert view is not None
        lines = format_span_tree_lines(view)
        text = "\n".join(lines)

        assert "attempt:" in text, "Failed single attempt must not be collapsed"
        assert "0/1" in text
        assert "failed" in text

    def test_attempt_count_in_overview(self) -> None:
        from ai_pipeline_core.database.snapshot._spans import build_span_tree_view
        from ai_pipeline_core.observability._tree_render import format_span_overview_lines

        tree, deployment_id = self._build_retry_tree()
        view = build_span_tree_view(tree, deployment_id)
        assert view is not None

        lines = format_span_overview_lines(view)
        text = "\n".join(lines)

        assert "Attempts: 2" in text

    def test_attempt_spans_excluded_from_sibling_filename_index(self) -> None:
        from ai_pipeline_core.database.snapshot._spans import build_span_tree_view
        from ai_pipeline_core.observability._tree_render import format_span_tree_lines

        tree, deployment_id = self._build_retry_tree()
        view = build_span_tree_view(tree, deployment_id)
        assert view is not None

        lines = format_span_tree_lines(view, include_filenames=True)

        assert not any("attempt" in line.lower() and ".json" in line for line in lines)


# ===========================================================================
# Event reconstruction tests
# ===========================================================================


class TestEventReconstruction:
    @pytest.mark.asyncio
    async def test_reconstruction_walks_through_attempt_to_flow(self) -> None:
        database = _MemoryDatabase()
        root_id = uuid4()
        t0 = datetime(2026, 3, 17, 12, 0, tzinfo=UTC)

        deploy = _make_span_record(
            deployment_id=root_id,
            root_deployment_id=root_id,
            kind=SpanKind.DEPLOYMENT,
            name="Deploy",
            started_at=t0,
            ended_at=t0 + timedelta(seconds=20),
            meta_json=json.dumps({
                "deployment_class": "TestPipeline",
                "flow_plan": [{"name": "Flow1", "flow_class": "TestFlow", "step": 1, "expected_tasks": [{"name": "Task1", "estimated_minutes": 1.0}]}],
            }),
        )
        flow = _make_span_record(
            span_id=uuid4(),
            parent_span_id=deploy.span_id,
            deployment_id=root_id,
            root_deployment_id=root_id,
            kind=SpanKind.FLOW,
            name="Flow1",
            started_at=t0 + timedelta(seconds=1),
            ended_at=t0 + timedelta(seconds=15),
            target="classmethod:mod:TestFlow.run",
            meta_json=json.dumps({"step": 1, "total_steps": 1, "expected_tasks": [{"name": "Task1", "estimated_minutes": 1.0}]}),
        )
        attempt = _make_span_record(
            span_id=uuid4(),
            parent_span_id=flow.span_id,
            deployment_id=root_id,
            root_deployment_id=root_id,
            kind=SpanKind.ATTEMPT,
            name="attempt-0",
            started_at=t0 + timedelta(seconds=2),
            ended_at=t0 + timedelta(seconds=14),
            meta_json=json.dumps({"attempt": 0, "max_attempts": 2}),
        )
        task = _make_span_record(
            span_id=uuid4(),
            parent_span_id=attempt.span_id,
            deployment_id=root_id,
            root_deployment_id=root_id,
            kind=SpanKind.TASK,
            name="Task1",
            started_at=t0 + timedelta(seconds=3),
            ended_at=t0 + timedelta(seconds=10),
            target="classmethod:mod:Task1.run",
            meta_json=json.dumps({"attempt": 0}),
        )

        for span in (deploy, flow, attempt, task):
            await database.insert_span(span)

        events = await _reconstruct_lifecycle_events(database, root_id)

        task_started = next((e for e in events if e.event_type == "task.started"), None)
        assert task_started is not None
        assert task_started.data["parent_span_id"] == str(flow.span_id)

    @pytest.mark.asyncio
    async def test_attempt_spans_produce_no_lifecycle_events(self) -> None:
        database = _MemoryDatabase()
        root_id = uuid4()
        t0 = datetime(2026, 3, 17, 12, 0, tzinfo=UTC)

        deploy = _make_span_record(
            deployment_id=root_id,
            root_deployment_id=root_id,
            kind=SpanKind.DEPLOYMENT,
            name="Deploy",
            started_at=t0,
            ended_at=t0 + timedelta(seconds=10),
            meta_json=json.dumps({"deployment_class": "TestPipeline", "flow_plan": []}),
        )
        attempt = _make_span_record(
            parent_span_id=deploy.span_id,
            deployment_id=root_id,
            root_deployment_id=root_id,
            kind=SpanKind.ATTEMPT,
            name="attempt-0",
            started_at=t0 + timedelta(seconds=1),
            ended_at=t0 + timedelta(seconds=5),
            meta_json=json.dumps({"attempt": 0, "max_attempts": 1}),
        )

        for span in (deploy, attempt):
            await database.insert_span(span)

        events = await _reconstruct_lifecycle_events(database, root_id)
        event_types = {e.event_type for e in events}

        assert not any("attempt" in t for t in event_types)


# ===========================================================================
# Replay exclusion tests
# ===========================================================================


class TestReplayExclusion:
    @pytest.mark.asyncio
    async def test_find_experiment_span_ids_excludes_attempt_spans(self) -> None:
        database = _MemoryDatabase()
        root_id = uuid4()
        t0 = datetime(2026, 3, 17, 12, 0, tzinfo=UTC)

        task = _make_span_record(
            deployment_id=root_id,
            root_deployment_id=root_id,
            kind=SpanKind.TASK,
            name="Task1",
            started_at=t0,
            ended_at=t0 + timedelta(seconds=5),
            target="classmethod:mod:Task1.run",
        )
        attempt = _make_span_record(
            parent_span_id=task.span_id,
            deployment_id=root_id,
            root_deployment_id=root_id,
            kind=SpanKind.ATTEMPT,
            name="attempt-0",
            started_at=t0 + timedelta(seconds=1),
            ended_at=t0 + timedelta(seconds=4),
        )

        await database.insert_span(task)
        await database.insert_span(attempt)

        span_ids = await find_experiment_span_ids(database, root_id)
        kinds = {database._spans[sid].kind for sid in span_ids}

        assert SpanKind.ATTEMPT not in kinds
        assert SpanKind.TASK in kinds

    @pytest.mark.asyncio
    async def test_find_experiment_span_ids_with_kind_none_excludes_attempt(self) -> None:
        database = _MemoryDatabase()
        root_id = uuid4()
        t0 = datetime(2026, 3, 17, 12, 0, tzinfo=UTC)

        conversation = _make_span_record(
            deployment_id=root_id,
            root_deployment_id=root_id,
            kind=SpanKind.CONVERSATION,
            name="Conv",
            started_at=t0,
            ended_at=t0 + timedelta(seconds=2),
            target="decoded_method:ai_pipeline_core.llm.conversation:Conversation.send",
        )
        attempt = _make_span_record(
            deployment_id=root_id,
            root_deployment_id=root_id,
            kind=SpanKind.ATTEMPT,
            name="attempt-0",
            started_at=t0 + timedelta(seconds=1),
            ended_at=t0 + timedelta(seconds=2),
        )

        await database.insert_span(conversation)
        await database.insert_span(attempt)

        span_ids = await find_experiment_span_ids(database, root_id, kind=None)
        returned_kinds = {database._spans[sid].kind for sid in span_ids}

        assert SpanKind.ATTEMPT not in returned_kinds


# ===========================================================================
# Flow retry ATTEMPT span tests
# ===========================================================================


class TestFlowRetryAttemptSpans:
    @pytest.mark.asyncio
    async def test_flow_retry_success_creates_attempt_spans(self) -> None:
        from ai_pipeline_core.deployment._deployment_runtime import _resolve_flow_arguments, _run_flow_with_retries

        _FailOnceFlow._attempt_count = 0
        database = _MemoryDatabase()
        ctx = _make_context(database)
        parent_ctx = SpanContext(span_id=ctx.span_id, parent_span_id=None)
        flow = _FailOnceFlow()

        with set_execution_context(ctx):
            await _run_flow_with_retries(
                flow_instance=flow,
                flow_class=_FailOnceFlow,
                flow_name="FailOnceFlow",
                resolved_kwargs=_resolve_flow_arguments(_FailOnceFlow, [_make_input()], FlowOptions()),
                current_exec_ctx=ctx,
                active_handles_before=set(),
                step=1,
                total_steps=1,
                deployment_flow_retries=1,
                deployment_flow_retry_delay_seconds=0,
                parent_span_ctx=parent_ctx,
            )

        attempt_spans = sorted(
            (s for s in database._spans.values() if s.kind == SpanKind.ATTEMPT),
            key=lambda s: s.sequence_no,
        )
        assert len(attempt_spans) == 2
        assert attempt_spans[0].status == SpanStatus.FAILED
        assert attempt_spans[1].status == SpanStatus.COMPLETED
        # ATTEMPT spans must parent to the current span in execution context
        assert all(s.parent_span_id == ctx.span_id for s in attempt_spans)
        # ATTEMPT span meta must carry attempt index and max_attempts
        assert json.loads(attempt_spans[0].meta_json) == {"attempt": 0, "max_attempts": 2}
        assert json.loads(attempt_spans[1].meta_json) == {"attempt": 1, "max_attempts": 2}
        assert len(parent_ctx._retry_errors) == 1
        assert parent_ctx._retry_errors[0]["error_message"] == "flow transient failure"
        assert parent_ctx._retry_errors[0]["attempt_span_id"] == str(attempt_spans[0].span_id)

    @pytest.mark.asyncio
    async def test_flow_all_attempts_fail_records_all_errors(self) -> None:
        from ai_pipeline_core.deployment._deployment_runtime import _resolve_flow_arguments, _run_flow_with_retries

        database = _MemoryDatabase()
        ctx = _make_context(database)
        parent_ctx = SpanContext(span_id=ctx.span_id, parent_span_id=None)
        flow = _AlwaysFailFlow()

        with set_execution_context(ctx):
            with pytest.raises(RuntimeError, match="flow always fails"):
                await _run_flow_with_retries(
                    flow_instance=flow,
                    flow_class=_AlwaysFailFlow,
                    flow_name="AlwaysFailFlow",
                    resolved_kwargs=_resolve_flow_arguments(_AlwaysFailFlow, [_make_input()], FlowOptions()),
                    current_exec_ctx=ctx,
                    active_handles_before=set(),
                    step=1,
                    total_steps=1,
                    deployment_flow_retries=2,
                    deployment_flow_retry_delay_seconds=0,
                    parent_span_ctx=parent_ctx,
                )

        attempt_spans = [s for s in database._spans.values() if s.kind == SpanKind.ATTEMPT]
        assert len(attempt_spans) == 3
        assert all(s.status == SpanStatus.FAILED for s in attempt_spans)
        assert len(parent_ctx._retry_errors) == 3
        assert parent_ctx._retry_errors[-1]["will_retry"] is False

    @pytest.mark.asyncio
    async def test_no_retry_flow_failure_creates_failed_attempt_span(self) -> None:
        """A retries=0 flow that fails emits a failed ATTEMPT span but no retry_errors on the FLOW span."""
        from ai_pipeline_core.deployment._deployment_runtime import _resolve_flow_arguments, _run_flow_with_retries

        database = _MemoryDatabase()
        ctx = _make_context(database)
        parent_ctx = SpanContext(span_id=ctx.span_id, parent_span_id=None)

        with set_execution_context(ctx):
            with pytest.raises(RuntimeError, match="flow always fails"):
                await _run_flow_with_retries(
                    flow_instance=_AlwaysFailFlow(),
                    flow_class=_AlwaysFailFlow,
                    flow_name="AlwaysFailFlow",
                    resolved_kwargs=_resolve_flow_arguments(_AlwaysFailFlow, [_make_input()], FlowOptions()),
                    current_exec_ctx=ctx,
                    active_handles_before=set(),
                    step=1,
                    total_steps=1,
                    deployment_flow_retries=0,
                    deployment_flow_retry_delay_seconds=0,
                    parent_span_ctx=parent_ctx,
                )

        attempt_spans = [s for s in database._spans.values() if s.kind == SpanKind.ATTEMPT]
        assert len(attempt_spans) == 1
        assert attempt_spans[0].status == SpanStatus.FAILED
        assert attempt_spans[0].error_json
        error_data = json.loads(attempt_spans[0].error_json)
        assert error_data["type_name"] == "RuntimeError"
        assert "flow always fails" in error_data["message"]
        assert parent_ctx._retry_errors == []

    @pytest.mark.asyncio
    async def test_flow_single_attempt_span_even_without_retries(self) -> None:
        """Flows with retries=0 still emit one ATTEMPT span for uniform tree structure."""
        from ai_pipeline_core.deployment._deployment_runtime import _resolve_flow_arguments, _run_flow_with_retries

        database = _MemoryDatabase()
        ctx = _make_context(database)
        parent_ctx = SpanContext(span_id=ctx.span_id, parent_span_id=None)

        with set_execution_context(ctx):
            await _run_flow_with_retries(
                flow_instance=_NoRetryFlow(),
                flow_class=_NoRetryFlow,
                flow_name="NoRetryFlow",
                resolved_kwargs=_resolve_flow_arguments(_NoRetryFlow, [_make_input()], FlowOptions()),
                current_exec_ctx=ctx,
                active_handles_before=set(),
                step=1,
                total_steps=1,
                deployment_flow_retries=0,
                deployment_flow_retry_delay_seconds=0,
                parent_span_ctx=parent_ctx,
            )

        attempt_spans = [s for s in database._spans.values() if s.kind == SpanKind.ATTEMPT]
        assert len(attempt_spans) == 1
        assert attempt_spans[0].status == SpanStatus.COMPLETED
        assert json.loads(attempt_spans[0].meta_json) == {"attempt": 0, "max_attempts": 1}
        assert parent_ctx._retry_errors == []

    @pytest.mark.asyncio
    async def test_failed_flow_attempt_span_has_error_json(self) -> None:
        """Failed flow ATTEMPT spans must carry error_json with the exception details."""
        from ai_pipeline_core.deployment._deployment_runtime import _resolve_flow_arguments, _run_flow_with_retries

        database = _MemoryDatabase()
        ctx = _make_context(database)
        parent_ctx = SpanContext(span_id=ctx.span_id, parent_span_id=None)
        flow = _AlwaysFailFlow()

        with set_execution_context(ctx):
            with pytest.raises(RuntimeError):
                await _run_flow_with_retries(
                    flow_instance=flow,
                    flow_class=_AlwaysFailFlow,
                    flow_name="AlwaysFailFlow",
                    resolved_kwargs=_resolve_flow_arguments(_AlwaysFailFlow, [_make_input()], FlowOptions()),
                    current_exec_ctx=ctx,
                    active_handles_before=set(),
                    step=1,
                    total_steps=1,
                    deployment_flow_retries=1,
                    deployment_flow_retry_delay_seconds=0,
                    parent_span_ctx=parent_ctx,
                )

        failed_attempts = [s for s in database._spans.values() if s.kind == SpanKind.ATTEMPT and s.status == SpanStatus.FAILED]
        assert len(failed_attempts) == 2
        for span in failed_attempts:
            assert span.error_json, f"ATTEMPT span {span.span_id} has empty error_json"
            error_data = json.loads(span.error_json)
            assert error_data["type_name"] == "RuntimeError"
            assert "flow always fails" in error_data["message"]
