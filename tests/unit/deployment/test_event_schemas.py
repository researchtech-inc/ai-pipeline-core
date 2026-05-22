"""Tests for deployment event payload schemas and field structure."""

from typing import Any, cast

import pytest

from ai_pipeline_core import DeploymentResult, Document, FlowOptions, PipelineDeployment
from ai_pipeline_core.database import SpanKind, SpanStatus
from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.deployment._types import (
    FlowCompletedEvent,
    FlowFailedEvent,
    RunCompletedEvent,
    RunStartedEvent,
    TaskCompletedEvent,
    TaskFailedEvent,
    TaskStartedEvent,
    _MemoryPublisher,
)
from ai_pipeline_core.pipeline import PipelineFlow


class _GapInputDoc(Document):
    """Input document for event gap regressions."""


class _GapOutputDoc(Document):
    """Output document for event gap regressions."""


class _GapResult(DeploymentResult):
    """Result model for event gap regressions."""


class _GapFlow(PipelineFlow):
    name = "gap-flow"

    async def run(self, input_docs: tuple[_GapInputDoc, ...], options: FlowOptions) -> tuple[_GapOutputDoc, ...]:
        _ = options
        return (_GapOutputDoc.derive(derived_from=input_docs, name="gap-out.txt", content="ok"),)


class _FailingGapFlow(PipelineFlow):
    name = "failing-gap-flow"
    retries = 0

    async def run(self, input_docs: tuple[_GapInputDoc, ...], options: FlowOptions) -> tuple[_GapOutputDoc, ...]:
        _ = (input_docs, options)
        raise RuntimeError("intentional flow failure")


class _WrongShapeGapFlow(PipelineFlow):
    name = "wrong-shape-gap-flow"

    async def run(self, input_docs: tuple[_GapInputDoc, ...], options: FlowOptions) -> tuple[_GapOutputDoc, ...]:
        _ = options
        return cast(Any, [_GapOutputDoc.derive(derived_from=input_docs, name="gap-out.txt", content="ok")])


class _WrongItemGapFlow(PipelineFlow):
    name = "wrong-item-gap-flow"

    async def run(self, input_docs: tuple[_GapInputDoc, ...], options: FlowOptions) -> tuple[_GapOutputDoc, ...]:
        _ = (input_docs, options)
        return cast(Any, ("not-a-document",))


class _GapDeployment(PipelineDeployment[FlowOptions, _GapResult]):
    flow_retries = 0

    def build_flows(self, options: FlowOptions) -> list[PipelineFlow]:
        _ = options
        return [_GapFlow()]

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> _GapResult:
        _ = (run_id, documents, options)
        return _GapResult(success=True)


class _FailingGapDeployment(PipelineDeployment[FlowOptions, _GapResult]):
    flow_retries = 0

    def build_flows(self, options: FlowOptions) -> list[PipelineFlow]:
        _ = options
        return [_FailingGapFlow()]

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> _GapResult:
        _ = (run_id, documents, options)
        return _GapResult(success=False, error="failed")


class _WrongShapeGapDeployment(PipelineDeployment[FlowOptions, _GapResult]):
    flow_retries = 0

    def build_flows(self, options: FlowOptions) -> list[PipelineFlow]:
        _ = options
        return [_WrongShapeGapFlow()]

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> _GapResult:
        _ = (run_id, documents, options)
        return _GapResult(success=False, error="failed")


class _WrongItemGapDeployment(PipelineDeployment[FlowOptions, _GapResult]):
    flow_retries = 0

    def build_flows(self, options: FlowOptions) -> list[PipelineFlow]:
        _ = options
        return [_WrongItemGapFlow()]

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> _GapResult:
        _ = (run_id, documents, options)
        return _GapResult(success=False, error="failed")


def _make_input_doc() -> _GapInputDoc:
    return _GapInputDoc.create_root(name="input.txt", content="input", reason="event-gap-test")


@pytest.mark.asyncio
async def test_flow_plan_uses_flow_class_key() -> None:
    publisher = _MemoryPublisher()
    await _GapDeployment().run("gap-run", [_make_input_doc()], FlowOptions(), publisher=publisher)

    started = [event for event in publisher.events if isinstance(event, RunStartedEvent)]
    assert len(started) == 1
    flow_plan = started[0].flow_plan
    assert len(flow_plan) == 1
    assert "flow_class" in flow_plan[0]
    assert "class" not in flow_plan[0]


@pytest.mark.asyncio
async def test_run_completed_has_output_sha256s() -> None:
    publisher = _MemoryPublisher()
    await _GapDeployment().run("sha-run", [_make_input_doc()], FlowOptions(), publisher=publisher)

    completed = [event for event in publisher.events if isinstance(event, RunCompletedEvent)]
    assert len(completed) == 1
    assert isinstance(completed[0].output_document_sha256s, tuple)


def test_flow_failed_event_type_exists() -> None:
    from ai_pipeline_core.deployment._types import EventType, FlowFailedEvent

    assert hasattr(EventType, "FLOW_FAILED")
    event = FlowFailedEvent(
        run_id="r1",
        span_id="s1",
        root_deployment_id="root1",
        parent_deployment_task_id=None,
        flow_name="a",
        flow_class="A",
        step=1,
        total_steps=3,
        status="failed",
        error_message="boom",
    )
    assert event.error_message == "boom"


@pytest.mark.asyncio
async def test_flow_failed_event_emitted_on_flow_exception() -> None:
    publisher = _MemoryPublisher()
    with pytest.raises(RuntimeError, match="intentional flow failure"):
        await _FailingGapDeployment().run("flow-failed-run", [_make_input_doc()], FlowOptions(), publisher=publisher)

    flow_failed_events = [event for event in publisher.events if isinstance(event, FlowFailedEvent)]
    assert len(flow_failed_events) == 1
    assert flow_failed_events[0].flow_class == "_FailingGapFlow"
    assert flow_failed_events[0].step == 1
    assert flow_failed_events[0].error_message == "intentional flow failure"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("deployment", "error_pattern"),
    [
        (_WrongShapeGapDeployment(), "must return tuple\\[Document, \\.\\.\\.\\]"),
        (_WrongItemGapDeployment(), "returned non-Document items"),
    ],
)
async def test_invalid_flow_returns_emit_failed_event_and_fail_flow_node(
    deployment: PipelineDeployment[FlowOptions, _GapResult],
    error_pattern: str,
) -> None:
    publisher = _MemoryPublisher()
    database = _MemoryDatabase()

    with pytest.raises(TypeError, match=error_pattern):
        await deployment.run("invalid-flow-return", [_make_input_doc()], FlowOptions(), publisher=publisher, database=database)

    flow_failed_events = [event for event in publisher.events if isinstance(event, FlowFailedEvent)]
    assert len(flow_failed_events) == 1
    flow_spans = [span for span in database._spans.values() if span.kind == SpanKind.FLOW]
    assert len(flow_spans) == 1
    assert flow_spans[0].status == SpanStatus.FAILED


def test_task_events_have_step() -> None:
    started = TaskStartedEvent(
        run_id="r1",
        span_id="s1",
        root_deployment_id="root1",
        parent_deployment_task_id=None,
        flow_name="f",
        step=2,
        total_steps=3,
        status="running",
        task_name="t",
        task_class="T",
    )
    completed = TaskCompletedEvent(
        run_id="r1",
        span_id="s1",
        root_deployment_id="root1",
        parent_deployment_task_id=None,
        flow_name="f",
        step=2,
        total_steps=3,
        status="completed",
        task_name="t",
        task_class="T",
        duration_ms=10,
    )
    failed = TaskFailedEvent(
        run_id="r1",
        span_id="s1",
        root_deployment_id="root1",
        parent_deployment_task_id=None,
        flow_name="f",
        step=2,
        total_steps=3,
        status="failed",
        task_name="t",
        task_class="T",
        error_message="boom",
    )
    assert started.step == 2
    assert completed.step == 2
    assert failed.step == 2
    assert started.total_steps == 3
    assert completed.total_steps == 3
    assert failed.total_steps == 3


def test_task_events_have_span_id() -> None:
    started = TaskStartedEvent(
        run_id="r1",
        span_id="abc123",
        root_deployment_id="root1",
        parent_deployment_task_id=None,
        flow_name="f",
        step=1,
        total_steps=3,
        status="running",
        task_name="t",
        task_class="T",
    )
    completed = TaskCompletedEvent(
        run_id="r1",
        span_id="abc123",
        root_deployment_id="root1",
        parent_deployment_task_id=None,
        flow_name="f",
        step=1,
        total_steps=3,
        status="completed",
        task_name="t",
        task_class="T",
        duration_ms=10,
    )
    failed = TaskFailedEvent(
        run_id="r1",
        span_id="abc123",
        root_deployment_id="root1",
        parent_deployment_task_id=None,
        flow_name="f",
        step=1,
        total_steps=3,
        status="failed",
        task_name="t",
        task_class="T",
        error_message="boom",
    )
    assert started.span_id == "abc123"
    assert completed.span_id == "abc123"
    assert failed.span_id == "abc123"


def test_flow_completed_has_flow_class() -> None:
    event = FlowCompletedEvent(
        run_id="r1",
        span_id="s1",
        root_deployment_id="root1",
        parent_deployment_task_id=None,
        flow_name="f",
        flow_class="F",
        step=1,
        total_steps=2,
        status="completed",
        duration_ms=100,
    )
    assert event.flow_class == "F"


def test_document_ref_has_publicly_visible() -> None:
    from ai_pipeline_core.deployment._types import DocumentRef

    ref = DocumentRef(sha256="abc", class_name="MyDoc", name="f.txt", publicly_visible=True)
    assert ref.publicly_visible is True

    ref_default = DocumentRef(sha256="abc", class_name="MyDoc", name="f.txt")
    assert ref_default.publicly_visible is False


def test_document_ref_has_provenance() -> None:
    from ai_pipeline_core.deployment._types import DocumentRef

    ref = DocumentRef(
        sha256="abc",
        class_name="MyDoc",
        name="f.txt",
        derived_from=("sha1", "https://example.com"),
        triggered_by=("sha2",),
    )
    assert ref.derived_from == ("sha1", "https://example.com")
    assert ref.triggered_by == ("sha2",)

    ref_default = DocumentRef(sha256="abc", class_name="MyDoc", name="f.txt")
    assert ref_default.derived_from == ()
    assert ref_default.triggered_by == ()


def test_document_ref_asdict_includes_new_fields() -> None:
    from dataclasses import asdict

    from ai_pipeline_core.deployment._types import DocumentRef

    ref = DocumentRef(
        sha256="abc",
        class_name="MyDoc",
        name="f.txt",
        publicly_visible=True,
        derived_from=("sha1",),
        triggered_by=("sha2",),
    )
    d = asdict(ref)
    assert d["publicly_visible"] is True
    assert d["derived_from"] == ("sha1",)
    assert d["triggered_by"] == ("sha2",)
