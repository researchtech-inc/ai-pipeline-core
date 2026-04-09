"""Tests for span sinks and track_span lifecycle wiring."""

# pyright: reportPrivateUsage=false

import asyncio
import json
import logging
from types import MappingProxyType
from uuid import UUID, uuid7

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError as ClickHouseDatabaseError

from ai_pipeline_core.database import SpanKind, SpanStatus
from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.deployment._types import _NoopPublisher
from ai_pipeline_core.documents import Document
from ai_pipeline_core.logger._buffer import ExecutionLogBuffer
from ai_pipeline_core.logger._handler import ExecutionLogHandler
from ai_pipeline_core.pipeline._execution_context import ExecutionContext, get_sinks, set_execution_context
from ai_pipeline_core.pipeline._runtime_sinks import build_runtime_sinks
from ai_pipeline_core.pipeline._span_sink import DatabaseSpanSink
from ai_pipeline_core.pipeline._track_span import track_span
from ai_pipeline_core.pipeline.limits import _SharedStatus
from ai_pipeline_core.settings import settings


class _SpanInputDoc(Document):
    """Input document for span sink tests."""


class _SpanOutputDoc(Document):
    """Output document for span sink tests."""


class _RecordingMemoryDatabase(_MemoryDatabase):
    def __init__(self) -> None:
        super().__init__()
        self.inserted_spans: list[object] = []
        self.write_order: list[str] = []

    async def insert_span(self, span: object) -> None:
        self.inserted_spans.append(span)
        self.write_order.append("span")
        await super().insert_span(span)  # type: ignore[arg-type]

    async def save_blob_batch(self, blobs: list[object]) -> None:
        if blobs:
            self.write_order.append("blob")
        await super().save_blob_batch(blobs)  # type: ignore[arg-type]

    async def save_document_batch(self, records: list[object]) -> None:
        if records:
            self.write_order.append("document")
        await super().save_document_batch(records)  # type: ignore[arg-type]


class _FailingBlobDatabase(_RecordingMemoryDatabase):
    async def save_blob_batch(self, blobs: list[object]) -> None:
        _ = blobs
        raise OSError("blob-write-failed")


class _FailingSpanInsertDatabase(_RecordingMemoryDatabase):
    async def insert_span(self, span: object) -> None:
        _ = span
        raise OSError("span-insert-failed")


class _ClickHouseFailingBlobDatabase(_RecordingMemoryDatabase):
    async def save_blob_batch(self, blobs: list[object]) -> None:
        _ = blobs
        raise ClickHouseDatabaseError("blob-write-failed")


class _ClickHouseFailingSpanInsertDatabase(_RecordingMemoryDatabase):
    async def insert_span(self, span: object) -> None:
        _ = span
        raise ClickHouseDatabaseError("span-insert-failed")


class _FakeSink:
    def __init__(self) -> None:
        self.started: list[dict[str, object]] = []
        self.finished: list[dict[str, object]] = []

    async def on_span_started(self, **kwargs: object) -> None:
        self.started.append(dict(kwargs))

    async def on_span_finished(self, **kwargs: object) -> None:
        self.finished.append(dict(kwargs))


class _FailingSink:
    async def on_span_started(self, **kwargs: object) -> None:
        _ = kwargs
        raise RuntimeError("sink-start-failed")

    async def on_span_finished(self, **kwargs: object) -> None:
        _ = kwargs
        raise RuntimeError("sink-finish-failed")


class _SlowFinishSink:
    async def on_span_started(self, **kwargs: object) -> None:
        _ = kwargs

    async def on_span_finished(self, **kwargs: object) -> None:
        _ = kwargs
        await asyncio.sleep(1)


def _make_context(
    database: _MemoryDatabase,
    *,
    log_buffer: ExecutionLogBuffer | None = None,
) -> ExecutionContext:
    deployment_id = uuid7()
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
        deployment_name="span-sink-test",
        span_id=deployment_id,
        current_span_id=deployment_id,
        log_buffer=log_buffer,
    )


def _make_input_doc() -> _SpanInputDoc:
    return _SpanInputDoc.create_root(name="input.txt", content="input", reason="span-sink-test")


@pytest.mark.asyncio
async def test_database_span_sink_writes_running_then_terminal_rows() -> None:
    database = _RecordingMemoryDatabase()
    ctx = _make_context(database)
    sink = DatabaseSpanSink(database)
    input_doc = _make_input_doc()

    with set_execution_context(ctx):
        async with track_span(
            SpanKind.TASK,
            "task-span",
            "classmethod:test:Task.run",
            sinks=(sink,),
            encode_receiver={"mode": "constructor_args", "value": {"task_class": "Task"}},
            encode_input=(input_doc,),
            db=database,
            input_preview={"task_class": "Task", "input_documents": [input_doc.name]},
        ) as span_ctx:
            span_ctx._set_output_value((_SpanOutputDoc.derive(derived_from=(input_doc,), name="out.txt", content="output"),))

    assert len(database.inserted_spans) == 2
    started_span, finished_span = database.inserted_spans
    assert started_span.status == SpanStatus.RUNNING
    assert finished_span.status == SpanStatus.COMPLETED
    assert started_span.span_id == finished_span.span_id
    assert finished_span.version > started_span.version


@pytest.mark.asyncio
async def test_track_span_notifies_all_sinks_when_one_sink_fails() -> None:
    first = _FakeSink()
    second = _FakeSink()
    database = _RecordingMemoryDatabase()
    ctx = _make_context(database)

    with set_execution_context(ctx):
        async with track_span(
            SpanKind.OPERATION,
            "op",
            "function:test:op",
            sinks=(first, _FailingSink(), second),
            db=None,
            input_preview=None,
        ) as span_ctx:
            span_ctx._set_output_value(None)

    assert len(first.started) == 1
    assert len(second.started) == 1
    assert len(first.finished) == 1
    assert len(second.finished) == 1


@pytest.mark.asyncio
async def test_track_span_encodes_inputs_and_outputs_with_universal_codec() -> None:
    database = _RecordingMemoryDatabase()
    ctx = _make_context(database)
    input_doc = _make_input_doc()
    payload_bytes = b"binary-payload"

    with set_execution_context(ctx):
        async with track_span(
            SpanKind.OPERATION,
            "encode-test",
            "function:test:run",
            sinks=get_sinks(),
            encode_receiver={"mode": "decoded_state", "value": {"document": input_doc, "payload": payload_bytes}},
            encode_input={"documents": (input_doc,), "payload": payload_bytes},
            db=database,
            input_preview=None,
        ) as span_ctx:
            output_doc = _SpanOutputDoc.derive(derived_from=(input_doc,), name="out.txt", content="output")
            span_ctx._set_output_value((output_doc, payload_bytes))

    span = next(iter(database._spans.values()))
    receiver = json.loads(span.receiver_json)
    encoded_input = json.loads(span.input_json)
    encoded_output = json.loads(span.output_json)

    assert receiver["mode"] == "decoded_state"
    assert receiver["value"]["document"]["$type"] == "document_ref"
    assert receiver["value"]["payload"]["$type"] == "blob_ref"
    assert encoded_input["documents"]["$type"] == "tuple"
    assert encoded_output["$type"] == "tuple"
    assert span.input_document_shas == (input_doc.sha256,)
    assert len(span.input_blob_shas) == 1
    assert len(database._documents) >= 2


@pytest.mark.asyncio
async def test_track_span_captures_errors_and_marks_failed_status() -> None:
    database = _RecordingMemoryDatabase()
    ctx = _make_context(database)

    with set_execution_context(ctx):
        with pytest.raises(ValueError, match="boom"):
            async with track_span(
                SpanKind.OPERATION,
                "explode",
                "function:test:explode",
                sinks=get_sinks(),
                db=database,
                input_preview=None,
            ):
                raise ValueError("boom")

    span = next(iter(database._spans.values()))
    error_payload = json.loads(span.error_json)
    assert span.status == SpanStatus.FAILED
    assert span.error_type == "ValueError"
    assert error_payload["type_name"] == "ValueError"
    assert "boom" in error_payload["message"]


@pytest.mark.asyncio
async def test_track_span_persists_blobs_before_documents_before_span_rows() -> None:
    database = _RecordingMemoryDatabase()
    ctx = _make_context(database)
    input_doc = _make_input_doc()

    with set_execution_context(ctx):
        async with track_span(
            SpanKind.OPERATION,
            "ordering",
            "function:test:ordering",
            sinks=get_sinks(),
            encode_input={"payload": b"blob-bytes", "document": input_doc},
            db=database,
            input_preview=None,
        ) as span_ctx:
            span_ctx._set_output_value(None)

    assert database.write_order[:3] == ["blob", "document", "span"]


@pytest.mark.asyncio
async def test_track_span_consumes_log_summary_in_finally() -> None:
    root_logger = logging.getLogger()
    handler = next((item for item in root_logger.handlers if isinstance(item, ExecutionLogHandler)), None)
    added_handler = False
    if handler is None:
        handler = ExecutionLogHandler()
        root_logger.addHandler(handler)
        added_handler = True

    log_buffer = ExecutionLogBuffer()
    database = _RecordingMemoryDatabase()
    ctx = _make_context(database, log_buffer=log_buffer)
    span_id: UUID | None = None

    try:
        with set_execution_context(ctx):
            async with track_span(
                SpanKind.OPERATION,
                "logging",
                "function:test:logging",
                sinks=get_sinks(),
                db=database,
                input_preview=None,
            ) as span_ctx:
                span_id = span_ctx.span_id
                logging.getLogger("ai_pipeline_core.test").warning("warning from span")
                span_ctx._set_output_value(None)
    finally:
        if added_handler:
            root_logger.removeHandler(handler)

    assert span_id is not None
    span = database._spans[span_id]
    metrics = json.loads(span.metrics_json)
    assert metrics["log_summary"]["total"] >= 1
    assert metrics["log_summary"]["warnings"] >= 1
    assert log_buffer.get_summary(span_id) == {"total": 0, "warnings": 0, "errors": 0, "last_error": ""}


@pytest.mark.asyncio
async def test_track_span_preserves_original_exception_when_sink_cleanup_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    database = _RecordingMemoryDatabase()
    ctx = _make_context(database)

    monkeypatch.setattr("ai_pipeline_core.pipeline._track_span._SHUTDOWN_TIMEOUT_SECONDS", 0.01)

    with set_execution_context(ctx):
        with pytest.raises(ValueError, match="boom"):
            async with track_span(
                SpanKind.OPERATION,
                "slow-finish",
                "function:test:slow-finish",
                sinks=(_SlowFinishSink(),),
                db=database,
                input_preview=None,
            ) as span_ctx:
                span_ctx._set_output_value(None)
                raise ValueError("boom")


@pytest.mark.asyncio
async def test_track_span_logs_failed_when_status_is_failed_without_python_exception() -> None:
    root_logger = logging.getLogger()
    handler = next((item for item in root_logger.handlers if isinstance(item, ExecutionLogHandler)), None)
    added_handler = False
    if handler is None:
        handler = ExecutionLogHandler()
        root_logger.addHandler(handler)
        added_handler = True

    log_buffer = ExecutionLogBuffer()
    database = _RecordingMemoryDatabase()
    ctx = _make_context(database, log_buffer=log_buffer)

    try:
        with set_execution_context(ctx):
            async with track_span(
                SpanKind.OPERATION,
                "explicit-fail",
                "function:test:explicit-fail",
                sinks=get_sinks(),
                db=database,
                input_preview=None,
            ) as span_ctx:
                span_ctx.set_status(SpanStatus.FAILED)
                span_ctx._set_output_value(None)
    finally:
        if added_handler:
            root_logger.removeHandler(handler)

    logs = log_buffer.drain()
    assert any(log.event_type == "operation.failed" for log in logs)


@pytest.mark.asyncio
async def test_database_span_sink_extracts_previous_conversation_id() -> None:
    database = _RecordingMemoryDatabase()
    ctx = _make_context(database)
    previous_span_id = uuid7()

    with set_execution_context(ctx):
        async with track_span(
            SpanKind.CONVERSATION,
            "conversation",
            "decoded_method:test:Conversation.send",
            sinks=get_sinks(),
            encode_receiver={
                "mode": "decoded_state",
                "value": {"$type": "pydantic", "class_path": "test:Conversation", "data": {"_conversation_id": str(previous_span_id)}},
            },
            encode_input={"content": "hello"},
            db=database,
            input_preview=None,
        ) as span_ctx:
            span_ctx._set_output_value({"content": "ok"})

    span = next(iter(database._spans.values()))
    assert span.previous_conversation_id == previous_span_id


def test_span_context_set_metrics_rejects_unknown_fields() -> None:
    from ai_pipeline_core.pipeline._span_sink import SpanContext

    context = SpanContext(span_id=uuid7(), parent_span_id=None, input_preview=None)
    with pytest.raises(ValueError, match="Unknown span metric field"):
        context.set_metrics(unknown_metric=1)


@pytest.mark.asyncio
async def test_track_span_skips_database_sink_when_artifact_persistence_fails() -> None:
    database = _FailingBlobDatabase()
    other_sink = _FakeSink()
    ctx = _make_context(database)

    with set_execution_context(ctx):
        async with track_span(
            SpanKind.OPERATION,
            "artifact-failure",
            "function:test:artifact_failure",
            sinks=(DatabaseSpanSink(database), other_sink),
            encode_input={"payload": b"blob-bytes"},
            db=database,
            input_preview=None,
        ) as span_ctx:
            span_ctx._set_output_value(None)

    assert database.inserted_spans == []
    assert len(other_sink.started) == 1
    assert len(other_sink.finished) == 1


@pytest.mark.asyncio
async def test_track_span_skips_database_sink_when_clickhouse_artifact_persistence_fails() -> None:
    database = _ClickHouseFailingBlobDatabase()
    other_sink = _FakeSink()
    ctx = _make_context(database)

    with set_execution_context(ctx):
        async with track_span(
            SpanKind.OPERATION,
            "artifact-failure",
            "function:test:artifact_failure",
            sinks=(DatabaseSpanSink(database), other_sink),
            encode_input={"payload": b"blob-bytes"},
            db=database,
            input_preview=None,
        ) as span_ctx:
            span_ctx._set_output_value(None)

    assert database.inserted_spans == []
    assert ctx.recording_degraded is True
    assert len(other_sink.started) == 1
    assert len(other_sink.finished) == 1


@pytest.mark.asyncio
async def test_database_span_sink_logs_and_does_not_raise_when_insert_fails(caplog: pytest.LogCaptureFixture) -> None:
    database = _FailingSpanInsertDatabase()
    ctx = _make_context(database)

    caplog.set_level(logging.WARNING, logger="ai_pipeline_core.pipeline._span_sink")
    with set_execution_context(ctx):
        async with track_span(
            SpanKind.OPERATION,
            "insert-failure",
            "function:test:insert_failure",
            sinks=(DatabaseSpanSink(database),),
            db=database,
            input_preview=None,
        ) as span_ctx:
            span_ctx._set_output_value(None)

    assert "Database span insert failed for operation" in caplog.text


@pytest.mark.asyncio
async def test_database_span_sink_logs_and_degrades_when_clickhouse_insert_fails(caplog: pytest.LogCaptureFixture) -> None:
    database = _ClickHouseFailingSpanInsertDatabase()
    ctx = _make_context(database)

    caplog.set_level(logging.WARNING, logger="ai_pipeline_core.pipeline._span_sink")
    with set_execution_context(ctx):
        async with track_span(
            SpanKind.OPERATION,
            "insert-failure",
            "function:test:insert_failure",
            sinks=(DatabaseSpanSink(database),),
            db=database,
            input_preview=None,
        ) as span_ctx:
            span_ctx._set_output_value(None)
            span_ctx.set_meta(public="ok", _internal="secret")

    assert "Database span insert failed for operation" in caplog.text
    assert ctx.recording_degraded is True


@pytest.mark.asyncio
async def test_database_span_sink_strips_internal_meta_keys_before_serializing() -> None:
    database = _RecordingMemoryDatabase()
    ctx = _make_context(database)

    with set_execution_context(ctx):
        async with track_span(
            SpanKind.OPERATION,
            "meta-sanitized",
            "function:test:meta_sanitized",
            sinks=get_sinks(),
            db=database,
            input_preview=None,
        ) as span_ctx:
            span_ctx.set_meta(public="ok", _internal="secret")
            span_ctx.set_status(SpanStatus.COMPLETED.value)
            span_ctx._set_output_value(None)

    span = next(iter(database._spans.values()))
    assert json.loads(span.meta_json) == {"public": "ok"}
