# pyright: reportPrivateUsage=false
"""Tests for build_plan gating and resolve_document_inputs provenance preservation."""

import pytest
from pydantic import BaseModel

from ai_pipeline_core import DeploymentPlan, DeploymentResult, Document, FieldGate, FlowOptions, FlowStep, PipelineDeployment
from ai_pipeline_core.deployment._resolve import _DocumentInput, resolve_document_inputs
from ai_pipeline_core.deployment._types import FlowSkippedEvent, _MemoryPublisher
from ai_pipeline_core.pipeline import PipelineFlow, PipelineTask


class PlanInputDoc(Document):
    """Input for build_plan gate tests."""


class PlanMiddleDoc(Document):
    """Intermediate output for build_plan gate tests."""


class PlanOutputDoc(Document):
    """Final output for build_plan gate tests."""


class PlanDecisionModel(BaseModel):
    should_run: bool


class PlanDecisionDoc(Document[PlanDecisionModel]):
    """Typed control document used by FieldGate."""


class PlanResult(DeploymentResult):
    output_count: int = 0


class ToMiddleTask(PipelineTask):
    @classmethod
    async def run(cls, input_docs: tuple[PlanInputDoc, ...]) -> tuple[PlanMiddleDoc, ...]:
        return tuple(PlanMiddleDoc.derive(derived_from=(d,), name=f"mid_{d.name}", content="mid") for d in input_docs)


class ToOutputTask(PipelineTask):
    @classmethod
    async def run(cls, middle_docs: tuple[PlanMiddleDoc, ...]) -> tuple[PlanOutputDoc, ...]:
        return tuple(PlanOutputDoc.derive(derived_from=(d,), name=f"out_{d.name}", content="out") for d in middle_docs)


class _FalseDecisionProducerFlow(PipelineFlow):
    name = "false-decision-producer"

    async def run(self, input_docs: tuple[PlanInputDoc, ...], options: FlowOptions) -> tuple[PlanMiddleDoc | PlanDecisionDoc, ...]:
        _ = options
        middle = await ToMiddleTask.run(input_docs=input_docs)
        decision = PlanDecisionDoc.derive(
            derived_from=input_docs,
            name="decision.json",
            content=PlanDecisionModel(should_run=False),
        )
        return (*middle, decision)


class _TrueDecisionProducerFlow(PipelineFlow):
    name = "true-decision-producer"

    async def run(self, input_docs: tuple[PlanInputDoc, ...], options: FlowOptions) -> tuple[PlanMiddleDoc | PlanDecisionDoc, ...]:
        _ = options
        middle = await ToMiddleTask.run(input_docs=input_docs)
        decision = PlanDecisionDoc.derive(
            derived_from=input_docs,
            name="decision.json",
            content=PlanDecisionModel(should_run=True),
        )
        return (*middle, decision)


class ConsumerFlow(PipelineFlow):
    name = "consumer"
    ran = False

    async def run(self, middle_docs: tuple[PlanMiddleDoc, ...], options: FlowOptions) -> tuple[PlanOutputDoc, ...]:
        _ = options
        type(self).ran = True
        return await ToOutputTask.run(middle_docs=middle_docs)


class _SkipWhenGateFalseDeployment(PipelineDeployment[FlowOptions, PlanResult]):
    flow_retries = 0

    def build_plan(self, options: FlowOptions) -> DeploymentPlan:
        _ = options
        return DeploymentPlan(
            steps=(
                FlowStep(_FalseDecisionProducerFlow()),
                FlowStep(ConsumerFlow(), run_if=FieldGate(PlanDecisionDoc, "should_run", op="truthy", on_missing="skip")),
            )
        )

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> PlanResult:
        _ = (run_id, options)
        return PlanResult(success=True, output_count=len(documents))


class _RunWhenGateTrueDeployment(PipelineDeployment[FlowOptions, PlanResult]):
    flow_retries = 0

    def build_plan(self, options: FlowOptions) -> DeploymentPlan:
        _ = options
        return DeploymentPlan(
            steps=(
                FlowStep(_TrueDecisionProducerFlow()),
                FlowStep(ConsumerFlow(), run_if=FieldGate(PlanDecisionDoc, "should_run", op="truthy", on_missing="skip")),
            )
        )

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> PlanResult:
        _ = (run_id, options)
        return PlanResult(success=True, output_count=len(documents))


@pytest.mark.asyncio
async def test_field_gate_skips_when_false() -> None:
    publisher = _MemoryPublisher()
    ConsumerFlow.ran = False
    doc = PlanInputDoc.create_root(name="in.txt", content="x", reason="gate-false")

    result = await _SkipWhenGateFalseDeployment().run("gate-skip", [doc], FlowOptions(), publisher=publisher)

    assert result.success
    assert ConsumerFlow.ran is False

    skipped = [event for event in publisher.events if isinstance(event, FlowSkippedEvent)]
    assert len(skipped) == 1
    assert "run_if gate did not pass" in skipped[0].reason


@pytest.mark.asyncio
async def test_field_gate_continues_when_true() -> None:
    publisher = _MemoryPublisher()
    ConsumerFlow.ran = False
    doc = PlanInputDoc.create_root(name="in.txt", content="x", reason="gate-true")

    result = await _RunWhenGateTrueDeployment().run("gate-run", [doc], FlowOptions(), publisher=publisher)

    assert result.success
    assert ConsumerFlow.ran is True

    skipped = [event for event in publisher.events if isinstance(event, FlowSkippedEvent)]
    assert skipped == []


class ResolveInputDoc(Document):
    """Document type for resolve test."""


@pytest.mark.asyncio
async def test_resolve_preserves_derived_from_on_input() -> None:
    """DocumentInput with derived_from preserves provenance through resolution."""
    root = ResolveInputDoc.create_root(name="root.txt", content="root", reason="test")
    inputs = [_DocumentInput(content="hello", name="x.txt", class_name="ResolveInputDoc", derived_from=(root.sha256,))]
    result = await resolve_document_inputs(inputs, [ResolveInputDoc])
    assert len(result) == 1
    assert result[0].derived_from == (root.sha256,)


@pytest.mark.asyncio
async def test_resolve_preserves_triggered_by_on_input() -> None:
    """DocumentInput with triggered_by preserves provenance through resolution."""
    trigger = ResolveInputDoc.create_root(name="trigger.txt", content="trigger", reason="test")
    inputs = [_DocumentInput(content="hello", name="x.txt", class_name="ResolveInputDoc", triggered_by=(trigger.sha256,))]
    result = await resolve_document_inputs(inputs, [ResolveInputDoc])
    assert len(result) == 1
    assert result[0].triggered_by == (trigger.sha256,)
