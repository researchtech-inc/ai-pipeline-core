# pyright: reportPrivateUsage=false
"""Tests for DeploymentPlan, FlowStep, FieldGate, and FlowOutputs."""

import logging
from typing import cast

import pytest
from pydantic import BaseModel

from ai_pipeline_core import (
    DeploymentPlan,
    DeploymentResult,
    Document,
    FieldGate,
    FlowOptions,
    FlowOutputs,
    FlowStep,
    PipelineDeployment,
)
from ai_pipeline_core.deployment._deployment_runtime import _execute_single_flow_attempt, _resolve_flow_arguments
from ai_pipeline_core.deployment._types import FlowSkippedEvent, _MemoryPublisher
from ai_pipeline_core.deployment.base import _evaluate_field_gate
from ai_pipeline_core.pipeline import PipelineFlow


class _PlanInputDoc(Document):
    """Input document for build-plan tests."""


class _PlanOutputDoc(Document):
    """Output document for build-plan tests."""


class _LoopBodyResultDoc(Document):
    """Output document produced by the loop body test flow."""


class _GateStateModel(BaseModel):
    should_run: bool
    status: str


class _GateStateDoc(Document[_GateStateModel]):
    """Typed control document used in FieldGate tests."""


class _UntypedGateDoc(Document):
    """Untyped document that must be rejected by FieldGate."""


class _ResolutionInputDoc(Document):
    """Input document for direct flow-argument resolution tests."""


class _ResolutionOptionalDoc(Document):
    """Optional document for direct flow-argument resolution tests."""


class _ResolutionOutputDoc(Document):
    """Output document for direct flow-argument resolution tests."""


class _UnusedIntermediateDoc(Document):
    """Intermediate document intentionally left unused downstream."""


class _PassthroughBaseDoc(Document):
    """Base type used to prove flow passthrough warnings."""


class _PassthroughDerivedDoc(_PassthroughBaseDoc):
    """Derived type used as both input instance and output type."""


class _PlanResult(DeploymentResult):
    output_count: int = 0


class _FirstFlow(PipelineFlow):
    async def run(self, input_docs: tuple[_PlanInputDoc, ...], options: FlowOptions) -> tuple[_GateStateDoc, ...]:
        _ = options
        return (_GateStateDoc.derive(derived_from=input_docs, name="state.json", content=_GateStateModel(should_run=True, status="ready")),)


class _SecondFlow(PipelineFlow):
    async def run(self, gate_states: tuple[_GateStateDoc, ...], options: FlowOptions) -> tuple[_PlanOutputDoc, ...]:
        _ = options
        return (_PlanOutputDoc.derive(derived_from=gate_states, name="out.txt", content="done"),)


class _FalseLoopDecisionFlow(PipelineFlow):
    async def run(self, input_docs: tuple[_PlanInputDoc, ...], options: FlowOptions) -> tuple[_GateStateDoc, ...]:
        _ = options
        return (_GateStateDoc.derive(derived_from=input_docs, name="loop-state.json", content=_GateStateModel(should_run=False, status="stop")),)


class _LoopBodyFlow(PipelineFlow):
    executions = 0

    async def run(self, gate_states: tuple[_GateStateDoc, ...], options: FlowOptions) -> tuple[_LoopBodyResultDoc, ...]:
        _ = (gate_states, options)
        type(self).executions += 1
        return (_LoopBodyResultDoc.derive(derived_from=gate_states, name="loop-body.txt", content="body"),)


class _TerminalFlow(PipelineFlow):
    async def run(self, gate_states: tuple[_GateStateDoc, ...], options: FlowOptions) -> tuple[_PlanOutputDoc, ...]:
        _ = options
        return (_PlanOutputDoc.derive(derived_from=gate_states, name="terminal.txt", content="terminal"),)


class _ResolutionFlow(PipelineFlow):
    async def run(
        self,
        required_input: _ResolutionInputDoc,
        optional_input: _ResolutionOptionalDoc | None,
        all_inputs: tuple[_ResolutionInputDoc, ...],
        options: FlowOptions,
    ) -> tuple[_ResolutionOutputDoc, ...]:
        _ = (required_input, optional_input, all_inputs, options)
        return (_ResolutionOutputDoc.create_root(name="resolved.txt", content="ok", reason="test"),)


class _ProducesUnusedFlow(PipelineFlow):
    async def run(self, input_docs: tuple[_PlanInputDoc, ...], options: FlowOptions) -> tuple[_UnusedIntermediateDoc, ...]:
        _ = options
        return (_UnusedIntermediateDoc.derive(derived_from=input_docs, name="unused.txt", content="unused"),)


class _ConsumesOriginalInputFlow(PipelineFlow):
    async def run(self, input_docs: tuple[_PlanInputDoc, ...], options: FlowOptions) -> tuple[_PlanOutputDoc, ...]:
        _ = options
        return (_PlanOutputDoc.derive(derived_from=input_docs, name="final.txt", content="done"),)


class _PassthroughWarningFlow(PipelineFlow):
    async def run(self, source: _PassthroughBaseDoc, options: FlowOptions) -> tuple[_PassthroughDerivedDoc, ...]:
        _ = options
        return (cast(_PassthroughDerivedDoc, source),)


class _DefaultPlanDeployment(PipelineDeployment[FlowOptions, _PlanResult]):
    flow_retries = 0

    def build_flows(self, options: FlowOptions) -> list[PipelineFlow]:
        _ = options
        return [_FirstFlow(), _SecondFlow()]

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> _PlanResult:
        _ = (run_id, options)
        return _PlanResult(success=True, output_count=len(documents))


class _GroupStopDeployment(PipelineDeployment[FlowOptions, _PlanResult]):
    flow_retries = 0

    def build_plan(self, options: FlowOptions) -> DeploymentPlan:
        _ = options
        return DeploymentPlan(
            steps=(
                FlowStep(_FalseLoopDecisionFlow(), group="loop"),
                FlowStep(_LoopBodyFlow(), group="loop"),
                FlowStep(_LoopBodyFlow(), group="loop"),
                FlowStep(_TerminalFlow()),
            ),
            group_stop_if={"loop": FieldGate(_GateStateDoc, "should_run", op="falsy", on_missing="run")},
        )

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> _PlanResult:
        _ = (run_id, options)
        return _PlanResult(success=True, output_count=len(documents))


class _InputControlledGroupStopDeployment(PipelineDeployment[FlowOptions, _PlanResult]):
    flow_retries = 0

    def build_plan(self, options: FlowOptions) -> DeploymentPlan:
        _ = options
        return DeploymentPlan(
            steps=(
                FlowStep(_LoopBodyFlow(), group="loop"),
                FlowStep(_LoopBodyFlow(), group="loop"),
                FlowStep(_TerminalFlow()),
            ),
            group_stop_if={"loop": FieldGate(_GateStateDoc, "should_run", op="falsy", on_missing="run")},
        )

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> _PlanResult:
        _ = (run_id, options)
        return _PlanResult(success=True, output_count=len(documents))


class _UnusedOutputDeployment(PipelineDeployment[FlowOptions, _PlanResult]):
    flow_retries = 0

    def build_plan(self, options: FlowOptions) -> DeploymentPlan:
        _ = options
        return DeploymentPlan(steps=(FlowStep(_ProducesUnusedFlow()), FlowStep(_ConsumesOriginalInputFlow())))

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> _PlanResult:
        _ = (run_id, options)
        return _PlanResult(success=True, output_count=len(documents))


def test_default_build_plan_wraps_all_flows() -> None:
    plan = _DefaultPlanDeployment().build_plan(FlowOptions())

    assert isinstance(plan, DeploymentPlan)
    assert [type(step.flow).__name__ for step in plan.steps] == ["_FirstFlow", "_SecondFlow"]
    assert all(step.run_if is None for step in plan.steps)


def test_flow_outputs_latest_and_all() -> None:
    first = _PlanOutputDoc.create_root(name="a.txt", content="first", reason="test")
    second = _PlanOutputDoc.create_root(name="b.txt", content="second", reason="test")
    outputs = FlowOutputs((first, second))

    assert outputs.latest(_PlanOutputDoc) is second
    assert outputs.all(_PlanOutputDoc) == (first, second)


def test_field_gate_truthy_falsy_eq_ne() -> None:
    gate_doc = _GateStateDoc.create_root(
        name="state.json",
        content=_GateStateModel(should_run=True, status="ready"),
        reason="gate-test",
    )

    assert _evaluate_field_gate(FieldGate(_GateStateDoc, "should_run", op="truthy"), [gate_doc]) is True
    assert _evaluate_field_gate(FieldGate(_GateStateDoc, "should_run", op="falsy"), [gate_doc]) is False
    assert _evaluate_field_gate(FieldGate(_GateStateDoc, "status", op="eq", value="ready"), [gate_doc]) is True
    assert _evaluate_field_gate(FieldGate(_GateStateDoc, "status", op="ne", value="stop"), [gate_doc]) is True


def test_field_gate_on_missing_run_and_skip() -> None:
    run_gate = FieldGate(_GateStateDoc, "should_run", op="truthy", on_missing="run")
    skip_gate = FieldGate(_GateStateDoc, "should_run", op="truthy", on_missing="skip")

    assert _evaluate_field_gate(run_gate, []) is True
    assert _evaluate_field_gate(skip_gate, []) is False
    assert _evaluate_field_gate(run_gate, [], context="stop") is False
    assert _evaluate_field_gate(skip_gate, [], context="stop") is True


def test_resolve_flow_arguments_takes_latest_singleton_and_all_collection_matches() -> None:
    first = _ResolutionInputDoc.create_root(name="first.txt", content="1", reason="test")
    second = _ResolutionInputDoc.create_root(name="second.txt", content="2", reason="test")
    optional = _ResolutionOptionalDoc.create_root(name="optional.txt", content="3", reason="test")

    resolved = _resolve_flow_arguments(_ResolutionFlow, [first, optional, second], FlowOptions())

    assert resolved["required_input"] is second
    assert resolved["optional_input"] is optional
    assert resolved["all_inputs"] == (first, second)
    assert isinstance(resolved["options"], FlowOptions)


def test_deployment_warns_on_unused_nonterminal_flow_outputs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="ai_pipeline_core.deployment.base")
    input_doc = _PlanInputDoc.create_root(name="input.txt", content="in", reason="unused-output-test")

    result = _UnusedOutputDeployment().run_local(
        run_id="unused-output-warning",
        documents=[input_doc],
        options=FlowOptions(),
    )

    assert result.success is True
    assert "returns document type(s) that no downstream flow or gate consumes" in caplog.text
    assert "_UnusedIntermediateDoc" in caplog.text


@pytest.mark.asyncio
async def test_flow_returning_unchanged_input_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="ai_pipeline_core.deployment._deployment_runtime")
    source = _PassthroughDerivedDoc.create_root(name="same.txt", content="same", reason="passthrough-test")
    flow = _PassthroughWarningFlow()

    result = await _execute_single_flow_attempt(
        type(flow),
        flow,
        {"source": source, "options": FlowOptions()},
    )

    assert result == (source,)
    assert "returned input document(s) unchanged" in caplog.text
    assert "'same.txt'" in caplog.text


def test_resolve_flow_arguments_optional_singleton_becomes_none() -> None:
    input_doc = _ResolutionInputDoc.create_root(name="first.txt", content="1", reason="test")

    resolved = _resolve_flow_arguments(_ResolutionFlow, [input_doc], FlowOptions())

    assert resolved["required_input"] is input_doc
    assert resolved["optional_input"] is None
    assert resolved["all_inputs"] == (input_doc,)


def test_resolve_flow_arguments_missing_required_singleton_raises() -> None:
    optional = _ResolutionOptionalDoc.create_root(name="optional.txt", content="3", reason="test")

    with pytest.raises(TypeError, match="no matching document was found in the blackboard"):
        _resolve_flow_arguments(_ResolutionFlow, [optional], FlowOptions())


def test_deployment_plan_rejects_invalid_steps() -> None:
    with pytest.raises(ValueError, match="at least one FlowStep"):
        DeploymentPlan(steps=())

    with pytest.raises(TypeError, match="FlowStep instances"):
        DeploymentPlan(steps=cast(tuple[FlowStep, ...], (object(),)))


def test_flow_step_rejects_invalid_inputs() -> None:
    with pytest.raises(TypeError, match="PipelineFlow instance"):
        FlowStep(cast(PipelineFlow, object()))

    with pytest.raises(TypeError, match="non-empty string or None"):
        FlowStep(_FirstFlow(), group="")


def test_field_gate_rejects_untyped_document() -> None:
    with pytest.raises(TypeError, match="typed Document subclass"):
        FieldGate(_UntypedGateDoc, "missing", op="truthy")


def test_field_gate_malformed_content_treated_as_missing(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    valid_doc = _GateStateDoc.create_root(
        name="state.json",
        content=_GateStateModel(should_run=True, status="ready"),
        reason="gate-test",
    )
    malformed_doc = _GateStateDoc(
        name=valid_doc.name,
        content=b"{broken",
        description=valid_doc.description,
        summary=valid_doc.summary,
        derived_from=valid_doc.derived_from,
        triggered_by=valid_doc.triggered_by,
        attachments=valid_doc.attachments,
    )

    gate = FieldGate(_GateStateDoc, "should_run", op="truthy", on_missing="skip")

    assert _evaluate_field_gate(gate, [malformed_doc]) is False
    assert "could not parse" in caplog.text


@pytest.mark.asyncio
async def test_group_stop_if_stops_remaining_group_steps() -> None:
    _LoopBodyFlow.executions = 0
    publisher = _MemoryPublisher()
    input_doc = _PlanInputDoc.create_root(name="input.txt", content="x", reason="group-stop")

    result = await _GroupStopDeployment().run("group-stop", [input_doc], FlowOptions(), publisher=publisher)

    assert result.success
    assert _LoopBodyFlow.executions == 0

    skipped = [event for event in publisher.events if isinstance(event, FlowSkippedEvent)]
    assert len(skipped) == 2
    assert all("group 'loop' stopped" in event.reason for event in skipped)


@pytest.mark.asyncio
async def test_group_stop_if_applies_to_initial_blackboard() -> None:
    _LoopBodyFlow.executions = 0
    publisher = _MemoryPublisher()
    input_doc = _PlanInputDoc.create_root(name="input.txt", content="x", reason="group-stop-input")
    stop_doc = _GateStateDoc.create_root(
        name="state.json",
        content=_GateStateModel(should_run=False, status="stop"),
        reason="preloaded stop control",
    )

    result = await _InputControlledGroupStopDeployment().run(
        "group-stop-input",
        [input_doc, stop_doc],
        FlowOptions(),
        publisher=publisher,
    )

    assert result.success
    assert _LoopBodyFlow.executions == 0

    skipped = [event for event in publisher.events if isinstance(event, FlowSkippedEvent)]
    assert len(skipped) == 2
    assert skipped[0].step == 1
    assert skipped[1].step == 2
