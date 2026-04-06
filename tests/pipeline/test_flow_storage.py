"""PipelineTask output-document linkage tests."""

# pyright: reportPrivateUsage=false

from types import MappingProxyType
from uuid import uuid7

import pytest

from ai_pipeline_core.database import SpanKind
from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.deployment._types import _NoopPublisher
from ai_pipeline_core.documents import Document
from ai_pipeline_core.pipeline import PipelineTask
from ai_pipeline_core.pipeline._execution_context import ExecutionContext, FlowFrame, set_execution_context
from ai_pipeline_core.pipeline._runtime_sinks import build_runtime_sinks
from ai_pipeline_core.pipeline.limits import _SharedStatus
from ai_pipeline_core.settings import settings


class InputDoc(Document):
    pass


class OutputDoc(Document):
    pass


class PersistTask(PipelineTask):
    @classmethod
    async def run(cls, input_docs: tuple[InputDoc, ...]) -> tuple[OutputDoc, ...]:
        source = input_docs[0]
        return (OutputDoc.derive(derived_from=(source,), name="out.txt", content="ok"),)


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
        sinks=build_runtime_sinks(database=database, settings_obj=settings),
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        deployment_name="test-pipeline",
        flow_frame=FlowFrame(
            name="flow",
            flow_class_name="Flow",
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
async def test_task_output_document_shas_are_queryable_from_span_tree() -> None:
    database = _MemoryDatabase()
    source = InputDoc.create_root(name="in.txt", content="hello", reason="flow-storage-test")
    with set_execution_context(_make_context(database)):
        outputs = await PersistTask.run((source,))

    task_span = next(span for span in database._spans.values() if span.kind == SpanKind.TASK)
    referencing_spans = await database.get_spans_referencing_document(outputs[0].sha256, kinds=[SpanKind.TASK])

    assert task_span.output_document_shas == (outputs[0].sha256,)
    assert [span.span_id for span in referencing_spans] == [task_span.span_id]
