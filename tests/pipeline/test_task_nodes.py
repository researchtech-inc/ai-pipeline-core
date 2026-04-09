"""Tests for task span creation and document persistence."""

# pyright: reportPrivateUsage=false

import json
import logging
from types import MappingProxyType
from uuid import UUID, uuid7

import pytest

from ai_pipeline_core.database import SpanKind, SpanStatus
from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.deployment._types import _NoopPublisher
from ai_pipeline_core.documents import Document
from ai_pipeline_core.logger._buffer import ExecutionLogBuffer
from ai_pipeline_core.logger._handler import ExecutionLogHandler
from ai_pipeline_core.logger._logging_config import setup_logging
from ai_pipeline_core.pipeline import PipelineTask
from ai_pipeline_core.pipeline._execution_context import ExecutionContext, FlowFrame, set_execution_context
from ai_pipeline_core.pipeline._runtime_sinks import build_runtime_sinks
from ai_pipeline_core.pipeline.limits import _SharedStatus
from ai_pipeline_core.settings import settings


class _RecordingSpanDatabase(_MemoryDatabase):
    def __init__(self) -> None:
        super().__init__()
        self.inserted_spans: list[object] = []

    async def insert_span(self, span: object) -> None:
        self.inserted_spans.append(span)
        await super().insert_span(span)  # type: ignore[arg-type]


class _NodeInDoc(Document):
    """Input document for task span tests."""


class _NodeOutDoc(Document):
    """Output document for task span tests."""


class _SimpleTask(PipelineTask):
    expected_cost = 0.25

    @classmethod
    async def run(cls, input_docs: tuple[_NodeInDoc, ...]) -> tuple[_NodeOutDoc, ...]:
        return (_NodeOutDoc.derive(derived_from=input_docs, name="out.txt", content="output"),)


class _FailingTask(PipelineTask):
    retries = 0

    @classmethod
    async def run(cls, input_docs: tuple[_NodeInDoc, ...]) -> tuple[_NodeOutDoc, ...]:
        _ = input_docs
        raise ValueError("deliberate task failure")


class _NestedChildTask(PipelineTask):
    @classmethod
    async def run(cls, input_docs: tuple[_NodeInDoc, ...]) -> tuple[_NodeOutDoc, ...]:
        return (_NodeOutDoc.derive(derived_from=input_docs, name="child.txt", content="child"),)


class _NestedParentTask(PipelineTask):
    @classmethod
    async def run(cls, input_docs: tuple[_NodeInDoc, ...]) -> tuple[_NodeOutDoc, ...]:
        return await _NestedChildTask.run(input_docs=input_docs)


class _SummaryTask(PipelineTask):
    @classmethod
    async def run(cls, input_docs: tuple[_NodeInDoc, ...]) -> tuple[_NodeOutDoc, ...]:
        return (
            _NodeOutDoc.derive(
                name="summary.txt",
                content="output",
                derived_from=(input_docs[0],),
                summary="Persist this summary",
            ),
        )


def _make_input() -> _NodeInDoc:
    return _NodeInDoc.create_root(name="input.txt", content="test-input", reason="task-span-test")


def _make_flow_frame() -> FlowFrame:
    return FlowFrame(
        name="test-flow",
        flow_class_name="TestFlow",
        step=1,
        total_steps=1,
        flow_minutes=(1.0,),
        completed_minutes=0.0,
        flow_params={},
    )


def _make_context_with_db(
    db: _MemoryDatabase,
    *,
    deployment_id: UUID | None = None,
    flow_span_id: UUID | None = None,
    log_buffer: ExecutionLogBuffer | None = None,
) -> ExecutionContext:
    resolved_deployment_id = deployment_id or uuid7()
    resolved_flow_span_id = flow_span_id or uuid7()
    return ExecutionContext(
        run_id="test-run",
        execution_id=None,
        publisher=_NoopPublisher(),
        limits=MappingProxyType({}),
        limits_status=_SharedStatus(),
        database=db,
        sinks=build_runtime_sinks(database=db, settings_obj=settings).span_sinks,
        deployment_id=resolved_deployment_id,
        root_deployment_id=resolved_deployment_id,
        deployment_name="test-pipeline",
        flow_frame=_make_flow_frame(),
        span_id=resolved_flow_span_id,
        current_span_id=resolved_flow_span_id,
        flow_span_id=resolved_flow_span_id,
        log_buffer=log_buffer,
    )


def _latest_task_span(database: _MemoryDatabase) -> object:
    spans = [span for span in database._spans.values() if span.kind == SpanKind.TASK]
    assert len(spans) == 1
    return spans[0]


@pytest.mark.asyncio
async def test_successful_task_creates_completed_task_span_with_description_and_document_links() -> None:
    database = _RecordingSpanDatabase()
    ctx = _make_context_with_db(database)
    input_doc = _make_input()
    with set_execution_context(ctx):
        result = await _SimpleTask.run((input_doc,))

    task_span = _latest_task_span(database)
    assert task_span.status == SpanStatus.COMPLETED
    assert task_span.parent_span_id == ctx.flow_span_id
    assert task_span.input_document_shas == (input_doc.sha256,)
    assert task_span.output_document_shas == (result[0].sha256,)
    assert "input_docs=[input.txt]" in task_span.description

    receiver_payload = json.loads(task_span.receiver_json)
    meta_payload = json.loads(task_span.meta_json)
    input_payload = json.loads(task_span.input_json)
    assert task_span.target == f"classmethod:{_SimpleTask.__module__}:{_SimpleTask.__qualname__}.run"
    assert receiver_payload["value"]["task_class"]["path"] == f"{_SimpleTask.__module__}:{_SimpleTask.__qualname__}"
    assert meta_payload["attempt"] == 0
    assert input_payload["input_docs"]["items"][0]["sha256"] == input_doc.sha256


@pytest.mark.asyncio
async def test_successful_task_detail_omits_remote_child_deployment_id() -> None:
    database = _RecordingSpanDatabase()
    with set_execution_context(_make_context_with_db(database)):
        await _SimpleTask.run((_make_input(),))

    task_span = _latest_task_span(database)
    assert "remote_child_deployment_id" not in json.loads(task_span.meta_json)


@pytest.mark.asyncio
async def test_task_completion_includes_log_summary_and_prunes_buffer_state() -> None:
    setup_logging(level="INFO")
    root_logger = logging.getLogger()
    handler = next((item for item in root_logger.handlers if isinstance(item, ExecutionLogHandler)), None)
    added_handler = False
    if handler is None:
        handler = ExecutionLogHandler()
        root_logger.addHandler(handler)
        added_handler = True

    buffer = ExecutionLogBuffer()
    database = _RecordingSpanDatabase()
    ctx = _make_context_with_db(database, log_buffer=buffer)
    try:
        with set_execution_context(ctx):
            await _SimpleTask.run((_make_input(),))
    finally:
        if added_handler:
            root_logger.removeHandler(handler)

    task_span = _latest_task_span(database)
    metrics = json.loads(task_span.metrics_json)
    assert metrics["log_summary"]["total"] == 2
    assert metrics["log_summary"]["errors"] == 0
    assert buffer.get_summary(task_span.span_id) == {"total": 0, "warnings": 0, "errors": 0, "last_error": ""}


@pytest.mark.asyncio
async def test_failed_task_creates_failed_task_span_with_full_error_message() -> None:
    database = _RecordingSpanDatabase()
    with set_execution_context(_make_context_with_db(database)):
        with pytest.raises(ValueError, match="deliberate task failure"):
            await _FailingTask.run((_make_input(),))

    task_span = _latest_task_span(database)
    assert task_span.status == SpanStatus.FAILED
    assert task_span.error_type == "ValueError"
    assert task_span.error_message == "deliberate task failure"
    assert json.loads(task_span.meta_json)["attempt"] == 0


@pytest.mark.asyncio
async def test_nested_task_call_is_rejected_before_child_task_span() -> None:
    database = _RecordingSpanDatabase()
    with set_execution_context(_make_context_with_db(database)):
        with pytest.raises(RuntimeError, match="cannot be called from inside another task"):
            await _NestedParentTask.run((_make_input(),))

    task_spans = sorted(
        (span for span in database._spans.values() if span.kind == SpanKind.TASK),
        key=lambda span: span.sequence_no,
    )
    assert len(task_spans) == 1


@pytest.mark.asyncio
async def test_task_persists_documents_and_blobs_with_span_schema() -> None:
    database = _RecordingSpanDatabase()
    with set_execution_context(_make_context_with_db(database)):
        outputs = await _SummaryTask.run((_make_input(),))

    output = outputs[0]
    document_record = database._documents[output.sha256]
    blob = database._blobs[document_record.content_sha256]

    assert document_record.document_type == "_NodeOutDoc"
    assert document_record.summary == "Persist this summary"
    assert document_record.mime_type == output.mime_type
    assert document_record.size_bytes == output.size
    assert document_record.attachment_names == ()
    assert blob.content == output.content


@pytest.mark.asyncio
async def test_started_and_completed_rows_are_valid_span_records() -> None:
    database = _RecordingSpanDatabase()
    with set_execution_context(_make_context_with_db(database)):
        await _SimpleTask.run((_make_input(),))

    started_span, completed_span = [span for span in database.inserted_spans if getattr(span, "kind", None) == SpanKind.TASK]
    assert started_span.status == SpanStatus.RUNNING
    assert completed_span.status == SpanStatus.COMPLETED
    assert started_span.span_id == completed_span.span_id
    assert completed_span.version > started_span.version
    assert completed_span.input_json != ""
    assert completed_span.meta_json != ""


class _ConfiguredTask(PipelineTask):
    retries = 3
    timeout_seconds = 60
    retry_delay_seconds = 5

    @classmethod
    async def run(cls, input_docs: tuple[_NodeInDoc, ...]) -> tuple[_NodeOutDoc, ...]:
        return (_NodeOutDoc.derive(derived_from=input_docs, name="configured.txt", content="ok"),)


@pytest.mark.asyncio
async def test_task_detail_includes_retry_configuration() -> None:
    database = _RecordingSpanDatabase()
    with set_execution_context(_make_context_with_db(database)):
        await _ConfiguredTask.run((_make_input(),))

    task_span = _latest_task_span(database)
    meta = json.loads(task_span.meta_json)
    assert meta["retries"] == 3
    assert meta["timeout_seconds"] == 60
    assert meta["retry_delay_seconds"] == 5
