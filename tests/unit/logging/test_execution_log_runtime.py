"""Focused tests for execution log buffering and handler classification."""

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import logging
from types import MappingProxyType
from uuid import uuid4

from ai_pipeline_core.database import LogRecord
from ai_pipeline_core.deployment._types import _NoopPublisher
from ai_pipeline_core.logger._buffer import ExecutionLogBuffer
from ai_pipeline_core.logger._handler import ExecutionLogHandler, _should_add_sentry_breadcrumb
from ai_pipeline_core.pipeline._execution_context import ExecutionContext, set_execution_context
from ai_pipeline_core.pipeline.limits import _SharedStatus


def _make_log(**kwargs: object) -> LogRecord:
    deployment_id = kwargs.pop("_deployment_id", None) or uuid4()
    defaults: dict[str, object] = {
        "span_id": uuid4(),
        "deployment_id": deployment_id,
        "timestamp": datetime.now(UTC),
        "sequence_no": 0,
        "level": "INFO",
        "category": "framework",
        "logger_name": "ai_pipeline_core.tests",
        "message": "test log",
        "fields_json": "{}",
        "exception_text": "",
    }
    defaults.update(kwargs)
    return LogRecord(**defaults)  # type: ignore[arg-type]


def test_execution_log_buffer_sequences_summaries_and_flush_request() -> None:
    requested_flushes: list[str] = []
    deployment_id = uuid4()
    span_id = uuid4()
    buffer = ExecutionLogBuffer(flush_size=2, request_flush=lambda: requested_flushes.append("flush"))

    buffer.append(_make_log(span_id=span_id, _deployment_id=deployment_id, level="WARNING", message="warn"))
    buffer.append(_make_log(span_id=span_id, _deployment_id=deployment_id, level="ERROR", message="error"))

    drained = buffer.drain()
    assert [log.sequence_no for log in drained] == [0, 1]
    assert requested_flushes == ["flush"]
    assert buffer.get_summary(span_id) == {
        "total": 2,
        "warnings": 1,
        "errors": 1,
        "last_error": "error",
    }


def test_execution_log_handler_classifies_and_filters_levels() -> None:
    root_logger = logging.getLogger()
    original_root_level = root_logger.level
    root_logger.setLevel(logging.DEBUG)
    handler = next((item for item in root_logger.handlers if isinstance(item, ExecutionLogHandler)), None)
    added_handler = False
    if handler is None:
        handler = ExecutionLogHandler()
        root_logger.addHandler(handler)
        added_handler = True

    framework_logger = logging.getLogger("ai_pipeline_core.test_runtime")
    dependency_logger = logging.getLogger("httpx")
    application_logger = logging.getLogger("my_app.runtime")
    original_framework_level = framework_logger.level
    original_dependency_level = dependency_logger.level
    original_application_level = application_logger.level
    framework_logger.setLevel(logging.DEBUG)
    dependency_logger.setLevel(logging.DEBUG)
    application_logger.setLevel(logging.DEBUG)

    deployment_id = uuid4()
    buffer = ExecutionLogBuffer()
    ctx = ExecutionContext(
        run_id="test-run",
        execution_id=None,
        publisher=_NoopPublisher(),
        limits=MappingProxyType({}),
        limits_status=_SharedStatus(),
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        current_span_id=uuid4(),
        log_buffer=buffer,
    )
    try:
        with set_execution_context(ctx):
            framework_logger.debug("framework debug")
            dependency_logger.info("dependency info should be filtered")
            dependency_logger.warning("dependency warning")
            application_logger.debug("application debug should be filtered")
            application_logger.info("application info")
            logs = buffer.drain()
            assert {(log.category, log.message) for log in logs} == {
                ("framework", "framework debug"),
                ("dependency", "dependency warning"),
                ("application", "application info"),
            }
    finally:
        framework_logger.setLevel(original_framework_level)
        dependency_logger.setLevel(original_dependency_level)
        application_logger.setLevel(original_application_level)
        root_logger.setLevel(original_root_level)
        if added_handler:
            root_logger.removeHandler(handler)


def test_execution_log_handler_persists_structured_fields_and_exception_text() -> None:
    root_logger = logging.getLogger()
    original_root_level = root_logger.level
    root_logger.setLevel(logging.DEBUG)
    handler = next((item for item in root_logger.handlers if isinstance(item, ExecutionLogHandler)), None)
    added_handler = False
    if handler is None:
        handler = ExecutionLogHandler()
        root_logger.addHandler(handler)
        added_handler = True

    logger = logging.getLogger("ai_pipeline_core.test_runtime.fields")
    original_logger_level = logger.level
    logger.setLevel(logging.DEBUG)

    deployment_id = uuid4()
    span_id = uuid4()
    buffer = ExecutionLogBuffer()
    ctx = ExecutionContext(
        run_id="test-run",
        execution_id=None,
        publisher=_NoopPublisher(),
        limits=MappingProxyType({}),
        limits_status=_SharedStatus(),
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        current_span_id=span_id,
        log_buffer=buffer,
    )
    try:
        with set_execution_context(ctx):
            try:
                raise RuntimeError("boom")
            except RuntimeError:
                logger.exception(
                    "framework failure",
                    extra={
                        "event_type": "runtime.failed",
                        "fields_json": {"attempt": 2, "source": "handler-test"},
                    },
                )

            [stored_log] = buffer.drain()
            assert stored_log.deployment_id == deployment_id
            assert stored_log.span_id == span_id
            assert stored_log.event_type == "runtime.failed"
            assert json.loads(stored_log.fields_json) == {"attempt": 2, "source": "handler-test"}
            assert "RuntimeError: boom" in stored_log.exception_text
    finally:
        logger.setLevel(original_logger_level)
        root_logger.setLevel(original_root_level)
        if added_handler:
            root_logger.removeHandler(handler)


def test_execution_log_handler_ignores_records_without_execution_context() -> None:
    handler = ExecutionLogHandler()
    record = logging.LogRecord(
        name="my_app.runtime",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="outside context",
        args=(),
        exc_info=None,
    )

    handler.emit(record)


def test_execution_log_buffer_drops_oldest_when_capacity_is_exceeded(monkeypatch) -> None:
    monkeypatch.setattr("ai_pipeline_core.logger._buffer.MAX_PENDING_EXECUTION_LOGS", 2)
    span_id = uuid4()
    deployment_id = uuid4()
    buffer = ExecutionLogBuffer(max_pending_logs=2)

    buffer.append(_make_log(span_id=span_id, _deployment_id=deployment_id, message="first"))
    buffer.append(_make_log(span_id=span_id, _deployment_id=deployment_id, message="second"))
    buffer.append(_make_log(span_id=span_id, _deployment_id=deployment_id, message="third"))

    drained = buffer.drain()
    assert [log.message for log in drained] == ["second", "third"]
    assert buffer.consume_dropped_count() == 1


@dataclass(slots=True)
class _FakeRecord:
    """Minimal log record stub for testing _should_add_sentry_breadcrumb."""

    levelno: int


def test_lifecycle_warning_becomes_sentry_breadcrumb() -> None:
    """H3: lifecycle WARNING events pass through the breadcrumb filter."""
    record = _FakeRecord(logging.WARNING)
    assert _should_add_sentry_breadcrumb(category="lifecycle", record=record) is True


def test_lifecycle_info_excluded_from_sentry_breadcrumbs() -> None:
    """H3: lifecycle INFO events are excluded by the WARNING+ gate."""
    record = _FakeRecord(logging.INFO)
    assert _should_add_sentry_breadcrumb(category="lifecycle", record=record) is False


def test_framework_warning_excluded_from_sentry_breadcrumbs() -> None:
    """H3: framework WARNING events remain excluded from breadcrumbs."""
    record = _FakeRecord(logging.WARNING)
    assert _should_add_sentry_breadcrumb(category="framework", record=record) is False


def test_application_and_dependency_warnings_still_pass() -> None:
    """H3: existing application/dependency WARNING behavior unchanged."""
    record = _FakeRecord(logging.WARNING)
    assert _should_add_sentry_breadcrumb(category="application", record=record) is True
    assert _should_add_sentry_breadcrumb(category="dependency", record=record) is True
