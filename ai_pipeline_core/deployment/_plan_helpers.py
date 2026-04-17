"""Private plan and gate helpers shared by deployment execution."""

import logging
from typing import Any, Literal

from ai_pipeline_core.documents import Document

from ._types import DeploymentPlan, FieldGate

logger = logging.getLogger(__name__)

_MISSING_FIELD_VALUE = object()


def apply_group_stop_gates(plan: DeploymentPlan, accumulated_docs: list[Document], stopped_groups: set[str]) -> None:
    """Mark groups as stopped when their group_stop_if gate passes."""
    for group_name, stop_gate in plan.group_stop_if.items():
        if group_name in stopped_groups:
            continue
        if evaluate_field_gate(stop_gate, accumulated_docs, context="stop"):
            stopped_groups.add(group_name)


def evaluate_field_gate(
    gate: FieldGate,
    docs: list[Document],
    *,
    context: Literal["run", "stop"] = "run",
) -> bool:
    """Evaluate a FieldGate against the latest matching control document."""
    result = _missing_gate_result(gate, context=context)
    latest_match: Document[Any] | None = None
    for document in reversed(docs):
        if isinstance(document, gate.document_type):
            latest_match = document
            break

    if latest_match is not None:
        parsed: Any | None = None
        try:
            parsed = latest_match.parsed
        except (TypeError, ValueError) as exc:
            logger.warning(
                "FieldGate could not parse '%s' (%s) for %s.%s with op=%s. Treating the document as missing because .parsed failed: %s",
                latest_match.name,
                type(latest_match).__name__,
                gate.document_type.__name__,
                gate.field_name,
                gate.op,
                exc,
            )
        if parsed is not None:
            value = getattr(parsed, gate.field_name, _MISSING_FIELD_VALUE)
            if value is not _MISSING_FIELD_VALUE:
                if gate.op == "truthy":
                    result = bool(value)
                elif gate.op == "falsy":
                    result = not bool(value)
                elif gate.op == "eq":
                    result = value == gate.value
                elif gate.op == "ne":
                    result = value != gate.value
                else:
                    result = True
    return result


def gate_reason(gate: FieldGate) -> str:
    """Describe a FieldGate in human-readable form for logs and events."""
    if gate.op in {"eq", "ne"}:
        return f"{gate.document_type.__name__}.{gate.field_name} {gate.op} {gate.value!r}"
    return f"{gate.document_type.__name__}.{gate.field_name} {gate.op}"


def warn_on_unused_flow_outputs(deployment_name: str, plan: DeploymentPlan) -> None:
    """Warn when a non-terminal flow returns document types that nothing later consumes."""
    total_steps = len(plan.steps)
    for index, flow_step in enumerate(plan.steps[:-1]):
        produced_types = type(flow_step.flow).output_document_types
        if not produced_types:
            continue
        later_consumed = _later_consumed_document_types(plan, index)
        unused_types = [document_type for document_type in produced_types if not any(issubclass(document_type, consumed) for consumed in later_consumed)]
        if not unused_types:
            continue
        unused_names = ", ".join(document_type.__name__ for document_type in unused_types)
        logger.warning(
            "PipelineDeployment '%s' flow '%s' (step %d/%d) returns document type(s) that no downstream flow or gate consumes: %s. "
            "Flows should return only phase handoff artifacts. Remove the unused type, keep it task-local, or consume it in a later flow or gate.",
            deployment_name,
            flow_step.flow.name,
            index + 1,
            total_steps,
            unused_names,
        )


def _later_consumed_document_types(plan: DeploymentPlan, step_index: int) -> set[type[Document]]:
    """Return document types consumed by later flow inputs or gate evaluation."""
    consumed: set[type[Document]] = set()
    later_steps = plan.steps[step_index + 1 :]
    for later_step in later_steps:
        consumed.update(type(later_step.flow).input_document_types)
        if later_step.run_if is not None:
            consumed.add(later_step.run_if.document_type)
    remaining_groups = {step.group for step in later_steps if step.group is not None}
    for group_name, stop_gate in plan.group_stop_if.items():
        if group_name in remaining_groups:
            consumed.add(stop_gate.document_type)
    return consumed


def _missing_gate_result(gate: FieldGate, *, context: Literal["run", "stop"]) -> bool:
    if context == "run":
        return gate.on_missing == "run"
    return gate.on_missing == "skip"
