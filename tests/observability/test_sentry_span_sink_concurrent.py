"""Regression tests for Sentry span isolation and synchronous breadcrumbs."""

import asyncio
import importlib
import logging
from collections.abc import Generator
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest
import sentry_sdk

from ai_pipeline_core.database import SpanKind
from ai_pipeline_core.deployment._helpers import _ensure_execution_log_handler_installed
from ai_pipeline_core.logger._buffer import DEFAULT_LOG_BUFFER_FLUSH_SIZE, ExecutionLogBuffer
from ai_pipeline_core.logger._logging_config import setup_logging
from ai_pipeline_core.observability._sentry_init import _reset_for_testing, ensure_sentry_initialized
from ai_pipeline_core.observability._sentry_sink import SentrySpanSink
from ai_pipeline_core.pipeline._execution_context import pipeline_test_context, set_execution_context
from ai_pipeline_core.pipeline._span_types import SpanMetrics
from ai_pipeline_core.pipeline._track_span import track_span

_FAILURE_DELAY_SECONDS = 0.01
_TransportBase = importlib.import_module("sentry_sdk.transport").Transport


class _RecordingTransport(_TransportBase):
    def __init__(self) -> None:
        super().__init__()
        self.captured_events: list[dict[str, Any]] = []

    def capture_envelope(self, envelope: Any) -> None:
        for item in envelope.items:
            if item.type == "event":
                self.captured_events.append(item.payload.json)

    def flush(self, timeout: float, callback: Any | None = None) -> None:
        _ = timeout, callback

    def kill(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class _SentryRecorder:
    transport: _RecordingTransport


@pytest.fixture
def sentry_recorder(monkeypatch: pytest.MonkeyPatch) -> Generator[_SentryRecorder]:
    _reset_for_testing()
    setup_logging(level="INFO")
    _ensure_execution_log_handler_installed()
    transport = _RecordingTransport()
    real_init = sentry_sdk.init

    def _init_with_transport(**kwargs: Any) -> None:
        real_init(**kwargs, transport=transport)

    monkeypatch.setattr("ai_pipeline_core.observability._sentry_init.sentry_sdk.init", _init_with_transport)
    ensure_sentry_initialized("https://fake@localhost/1")
    yield _SentryRecorder(transport=transport)
    sentry_sdk.flush()
    _reset_for_testing()


def _event_for_run(events: list[dict[str, Any]], *, run_id: str) -> dict[str, Any]:
    for event in events:
        if event.get("tags", {}).get("run_id") == run_id:
            return event
    raise AssertionError(f"No captured event found for run_id={run_id!r}")


async def _capture_failed_task(
    *,
    run_id: str,
    message: str,
    logger_name: str,
    span_sink: SentrySpanSink,
    log_buffer: ExecutionLogBuffer,
) -> None:
    with ExitStack() as stack:
        ctx = stack.enter_context(pipeline_test_context(run_id=run_id))
        scope = stack.enter_context(sentry_sdk.isolation_scope())
        scope.set_tag("run_id", run_id)
        scope.set_tag("deployment_id", str(ctx.deployment_id))
        scope.set_tag("root_deployment_id", str(ctx.root_deployment_id))
        scope.set_tag("deployment_name", ctx.deployment_name)
        updated_ctx = replace(ctx, sinks=(span_sink,), log_buffer=log_buffer)
        stack.enter_context(set_execution_context(updated_ctx))

        with pytest.raises(RuntimeError, match=message):
            async with track_span(
                kind=SpanKind.TASK,
                name=f"task-{run_id}",
                target="",
                sinks=(span_sink,),
            ):
                logging.getLogger(logger_name).error(message)
                await asyncio.sleep(_FAILURE_DELAY_SECONDS)
                raise RuntimeError(message)


@pytest.mark.asyncio
async def test_synchronous_breadcrumb_present_with_production_buffer_defaults(
    sentry_recorder: _SentryRecorder,
) -> None:
    log_buffer = ExecutionLogBuffer(request_flush=lambda: None)
    span_sink = SentrySpanSink()

    await _capture_failed_task(
        run_id="buffer-defaults",
        message="dependency warning before failure",
        logger_name="httpx",
        span_sink=span_sink,
        log_buffer=log_buffer,
    )
    sentry_sdk.flush()

    event = _event_for_run(sentry_recorder.transport.captured_events, run_id="buffer-defaults")
    breadcrumb_messages = [crumb.get("message") for crumb in event.get("breadcrumbs", {}).get("values", [])]
    buffered_logs = log_buffer.drain()

    assert DEFAULT_LOG_BUFFER_FLUSH_SIZE == 500
    assert "dependency warning before failure" in breadcrumb_messages
    assert buffered_logs


@pytest.mark.asyncio
async def test_retry_breadcrumbs_replayed_on_final_capture(
    sentry_recorder: _SentryRecorder,
) -> None:
    span_sink = SentrySpanSink()
    log_buffer = ExecutionLogBuffer(request_flush=lambda: None)

    with ExitStack() as stack:
        ctx = stack.enter_context(pipeline_test_context(run_id="retry-run"))
        scope = stack.enter_context(sentry_sdk.isolation_scope())
        scope.set_tag("run_id", "retry-run")
        scope.set_tag("deployment_id", str(ctx.deployment_id))
        scope.set_tag("root_deployment_id", str(ctx.root_deployment_id))
        scope.set_tag("deployment_name", ctx.deployment_name)
        updated_ctx = replace(ctx, sinks=(span_sink,), log_buffer=log_buffer)
        stack.enter_context(set_execution_context(updated_ctx))

        with pytest.raises(RuntimeError, match="final failure"):
            async with track_span(
                kind=SpanKind.TASK,
                name="task-with-retries",
                target="",
                sinks=(span_sink,),
            ) as span_ctx:
                for attempt in range(1, 4):
                    span_ctx.record_retry_failure(
                        exc=RuntimeError(f"retry-{attempt}"),
                        attempt=attempt,
                        max_attempts=3,
                        will_retry=attempt < 3,
                    )
                raise RuntimeError("final failure")

    sentry_sdk.flush()

    event = _event_for_run(sentry_recorder.transport.captured_events, run_id="retry-run")
    breadcrumb_messages = [crumb.get("message") for crumb in event.get("breadcrumbs", {}).get("values", [])]

    assert breadcrumb_messages.count("retry-1 (retrying)") == 1
    assert breadcrumb_messages.count("retry-2 (retrying)") == 1
    assert breadcrumb_messages.count("retry-3") == 1


@pytest.mark.asyncio
async def test_concurrent_tasks_capture_only_their_own_breadcrumbs(
    sentry_recorder: _SentryRecorder,
) -> None:
    async def _run(run_id: str, message: str) -> None:
        await _capture_failed_task(
            run_id=run_id,
            message=message,
            logger_name="my.app",
            span_sink=SentrySpanSink(),
            log_buffer=ExecutionLogBuffer(request_flush=lambda: None),
        )

    await asyncio.gather(
        _run("run-a", "breadcrumb-a"),
        _run("run-b", "breadcrumb-b"),
    )
    sentry_sdk.flush()

    event_a = _event_for_run(sentry_recorder.transport.captured_events, run_id="run-a")
    event_b = _event_for_run(sentry_recorder.transport.captured_events, run_id="run-b")
    breadcrumbs_a = [crumb.get("message") for crumb in event_a.get("breadcrumbs", {}).get("values", [])]
    breadcrumbs_b = [crumb.get("message") for crumb in event_b.get("breadcrumbs", {}).get("values", [])]

    assert "breadcrumb-a" in breadcrumbs_a
    assert "breadcrumb-b" not in breadcrumbs_a
    assert "breadcrumb-b" in breadcrumbs_b
    assert "breadcrumb-a" not in breadcrumbs_b


_DUMMY_METRICS = SpanMetrics(time_taken_ms=0, log_summary={})
_DUMMY_FINISH_KWARGS: dict[str, Any] = {
    "ended_at": datetime(2026, 1, 1, tzinfo=UTC),
    "output_json": "",
    "error_json": "",
    "output_document_shas": frozenset(),
    "output_blob_shas": frozenset(),
    "output_preview": None,
    "metrics": _DUMMY_METRICS,
}


async def _register_span(sink: SentrySpanSink, span_id: UUID, kind: SpanKind, name: str) -> None:
    await sink.on_span_started(
        span_id=span_id,
        parent_span_id=None,
        kind=kind,
        name=name,
        target="",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        receiver_json="",
        input_json="",
        input_document_shas=frozenset(),
        input_blob_shas=frozenset(),
        input_preview=None,
    )


@pytest.mark.asyncio
async def test_exception_propagating_through_parents_captures_once_at_task() -> None:
    """H1: Same exception through ATTEMPT → LLM_ROUND → TASK → FLOW → DEPLOYMENT captures once."""
    sink = SentrySpanSink()
    error = RuntimeError("propagated")
    span_chain = [
        (SpanKind.ATTEMPT, "attempt-0"),
        (SpanKind.LLM_ROUND, "llm-call"),
        (SpanKind.TASK, "MyTask"),
        (SpanKind.FLOW, "MyFlow"),
        (SpanKind.DEPLOYMENT, "MyDeploy"),
    ]

    for i, (kind, name) in enumerate(span_chain):
        await _register_span(sink, UUID(int=i + 1), kind, name)

    capture_calls: list[BaseException] = []
    with patch.object(sentry_sdk, "capture_exception", side_effect=lambda e: capture_calls.append(e)):
        for i in range(len(span_chain)):
            await sink.on_span_finished(
                span_id=UUID(int=i + 1),
                error=error,
                meta={},
                **_DUMMY_FINISH_KWARGS,
            )

    assert len(capture_calls) == 1, f"Expected 1 capture, got {len(capture_calls)}"


@pytest.mark.asyncio
async def test_orchestration_error_bypassing_task_captures_at_deployment() -> None:
    """H1: Error that never reaches a TASK captures at DEPLOYMENT level."""
    sink = SentrySpanSink()
    error = RuntimeError("orchestration failure")

    await _register_span(sink, UUID(int=10), SpanKind.DEPLOYMENT, "Deploy")

    capture_calls: list[BaseException] = []
    with patch.object(sentry_sdk, "capture_exception", side_effect=lambda e: capture_calls.append(e)):
        await sink.on_span_finished(
            span_id=UUID(int=10),
            error=error,
            meta={},
            **_DUMMY_FINISH_KWARGS,
        )

    assert len(capture_calls) == 1


@pytest.mark.asyncio
async def test_low_level_span_kinds_never_capture() -> None:
    """H1: LLM_ROUND, TOOL_CALL, CONVERSATION, OPERATION never open Sentry issues."""
    excluded_kinds = [SpanKind.LLM_ROUND, SpanKind.TOOL_CALL, SpanKind.CONVERSATION, SpanKind.OPERATION]

    for kind in excluded_kinds:
        sink = SentrySpanSink()
        error = RuntimeError(f"error-{kind.value}")
        await _register_span(sink, UUID(int=1), kind, "test")

        calls: list[BaseException] = []
        with patch.object(sentry_sdk, "capture_exception", side_effect=lambda e, _c=calls: _c.append(e)):
            await sink.on_span_finished(
                span_id=UUID(int=1),
                error=error,
                meta={},
                **_DUMMY_FINISH_KWARGS,
            )

        assert calls == [], f"{kind.value} should not capture, but did"


@pytest.mark.asyncio
async def test_retry_breadcrumb_message_reflects_will_retry(sentry_recorder: _SentryRecorder) -> None:
    """H2: Single-attempt failures say 'attempt failed', not 'retry failure'."""
    ensure_sentry_initialized("https://fake@localhost/1")
    span_sink = SentrySpanSink()
    log_buffer = ExecutionLogBuffer(request_flush=lambda: None)

    with ExitStack() as stack:
        ctx = stack.enter_context(pipeline_test_context(run_id="breadcrumb-msg"))
        scope = stack.enter_context(sentry_sdk.isolation_scope())
        scope.set_tag("run_id", "breadcrumb-msg")
        scope.set_tag("deployment_id", str(ctx.deployment_id))
        scope.set_tag("root_deployment_id", str(ctx.root_deployment_id))
        scope.set_tag("deployment_name", ctx.deployment_name)
        updated_ctx = replace(ctx, sinks=(span_sink,), log_buffer=log_buffer)
        stack.enter_context(set_execution_context(updated_ctx))

        with pytest.raises(RuntimeError, match="final"):
            async with track_span(
                kind=SpanKind.TASK,
                name="task-msg-test",
                target="",
                sinks=(span_sink,),
            ) as span_ctx:
                span_ctx.record_retry_failure(
                    exc=RuntimeError("first-fail"),
                    attempt=0,
                    max_attempts=1,
                    will_retry=False,
                )
                span_ctx.record_retry_failure(
                    exc=RuntimeError("retry-fail"),
                    attempt=0,
                    max_attempts=3,
                    will_retry=True,
                )
                raise RuntimeError("final")

    sentry_sdk.flush()

    event = _event_for_run(sentry_recorder.transport.captured_events, run_id="breadcrumb-msg")
    breadcrumb_messages = [crumb.get("message") for crumb in event.get("breadcrumbs", {}).get("values", [])]

    assert "first-fail" in breadcrumb_messages
    assert "first-fail (retrying)" not in breadcrumb_messages
    assert "retry-fail (retrying)" in breadcrumb_messages
