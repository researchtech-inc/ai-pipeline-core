"""Tests for Sentry initialization and manual breadcrumb capture."""

import asyncio
import importlib
import logging
from collections.abc import Generator
from contextlib import ExitStack
from dataclasses import dataclass, replace
from typing import Any

import pytest
import sentry_sdk

from ai_pipeline_core.database import SpanKind
from ai_pipeline_core.deployment._helpers import _ensure_execution_log_handler_installed
from ai_pipeline_core.logger._buffer import ExecutionLogBuffer
from ai_pipeline_core.logger._logging_config import setup_logging
from ai_pipeline_core.observability._sentry_init import _reset_for_testing, ensure_sentry_initialized
from ai_pipeline_core.observability._sentry_sink import SentrySpanSink
from ai_pipeline_core.pipeline._execution_context import pipeline_test_context, set_execution_context
from ai_pipeline_core.pipeline._runtime_sinks import build_runtime_sinks
from ai_pipeline_core.pipeline._track_span import track_span
from ai_pipeline_core.settings import Settings

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
    init_calls: list[dict[str, Any]]


@pytest.fixture
def sentry_recorder(monkeypatch: pytest.MonkeyPatch) -> Generator[_SentryRecorder]:
    _reset_for_testing()
    setup_logging(level="INFO")
    _ensure_execution_log_handler_installed()
    transport = _RecordingTransport()
    real_init = sentry_sdk.init
    init_calls: list[dict[str, Any]] = []

    def _init_with_transport(**kwargs: Any) -> None:
        init_calls.append(dict(kwargs))
        real_init(**kwargs, transport=transport)

    monkeypatch.setattr("ai_pipeline_core.observability._sentry_init.sentry_sdk.init", _init_with_transport)
    yield _SentryRecorder(transport=transport, init_calls=init_calls)
    sentry_sdk.flush()
    _reset_for_testing()


async def _capture_failed_task(*, run_id: str, span_sink: SentrySpanSink, log_message: str | None) -> None:
    log_buffer = ExecutionLogBuffer(request_flush=lambda: None)

    with ExitStack() as stack:
        ctx = stack.enter_context(pipeline_test_context(run_id=run_id))
        scope = stack.enter_context(sentry_sdk.isolation_scope())
        scope.set_tag("run_id", run_id)
        scope.set_tag("deployment_id", str(ctx.deployment_id))
        scope.set_tag("root_deployment_id", str(ctx.root_deployment_id))
        scope.set_tag("deployment_name", ctx.deployment_name)
        updated_ctx = replace(ctx, sinks=(span_sink,), log_buffer=log_buffer)
        stack.enter_context(set_execution_context(updated_ctx))

        with pytest.raises(RuntimeError, match="boom"):
            async with track_span(
                kind=SpanKind.TASK,
                name="task-with-log",
                target="",
                sinks=(span_sink,),
            ):
                if log_message is not None:
                    logging.getLogger("my.app").error(log_message)
                await asyncio.sleep(0)
                raise RuntimeError("boom")


def test_ensure_sentry_initialized_uses_explicit_config(sentry_recorder: _SentryRecorder) -> None:
    ensure_sentry_initialized("https://fake@localhost/1")

    assert sentry_recorder.init_calls == [
        {
            "dsn": "https://fake@localhost/1",
            "default_integrations": False,
            "integrations": [],
            "traces_sample_rate": 0.0,
            "auto_enabling_integrations": False,
        }
    ]


def test_logger_error_without_run_scope_creates_no_event(sentry_recorder: _SentryRecorder) -> None:
    ensure_sentry_initialized("https://fake@localhost/1")

    logging.getLogger("my.app").error("x")
    sentry_sdk.flush()

    assert sentry_recorder.transport.captured_events == []


@pytest.mark.asyncio
async def test_logger_error_inside_run_scope_becomes_breadcrumb_not_event(sentry_recorder: _SentryRecorder) -> None:
    ensure_sentry_initialized("https://fake@localhost/1")
    span_sink = SentrySpanSink()

    await _capture_failed_task(run_id="run-scope", span_sink=span_sink, log_message="x")
    sentry_sdk.flush()

    assert len(sentry_recorder.transport.captured_events) == 1
    event = sentry_recorder.transport.captured_events[0]
    breadcrumb_messages = [crumb.get("message") for crumb in event.get("breadcrumbs", {}).get("values", [])]

    assert "x" in breadcrumb_messages
    assert event["tags"]["run_id"] == "run-scope"
    assert event["tags"]["deployment_id"]
    assert event["tags"]["root_deployment_id"]
    assert event["tags"]["span_kind"] == SpanKind.TASK.value


def test_sinks_not_constructed_when_dsn_empty() -> None:
    sinks = build_runtime_sinks(database=None, settings_obj=Settings(sentry_dsn=""))

    assert not any(isinstance(sink, SentrySpanSink) for sink in sinks.span_sinks)
    assert sinks.log_sinks == ()
