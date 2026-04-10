"""Event types and publisher protocols for pipeline lifecycle publishing."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal, Protocol, TypeVar, runtime_checkable

from ai_pipeline_core._lifecycle_events import DocumentRef, TaskCompletedEvent, TaskFailedEvent, TaskStartedEvent
from ai_pipeline_core.documents import Document
from ai_pipeline_core.pipeline import PipelineFlow

TDocument = TypeVar("TDocument", bound=Document[Any])
_CRASH_ERROR_TYPE = "ProcessCrashed"


@dataclass(frozen=True, slots=True)
class FieldGate:
    """Gate that checks a field value on the latest typed control document."""

    document_type: type[Document[Any]]
    field_name: str
    op: Literal["truthy", "falsy", "eq", "ne"]
    on_missing: Literal["run", "skip"] = "run"
    value: Any = None

    def __post_init__(self) -> None:
        content_type = self.document_type.get_content_type()
        if content_type is None:
            raise TypeError(
                f"FieldGate document_type must be a typed Document subclass, but {self.document_type.__name__} has no content type. "
                f"Declare it as 'class {self.document_type.__name__}(Document[MyModel]): ...' so .parsed.{self.field_name} is safe."
            )
        if self.document_type.content_is_list():
            raise TypeError(
                f"FieldGate document_type must wrap a single Pydantic model, but {self.document_type.__name__} is Document[list[{content_type.__name__}]]. "
                f"Use a control document with a single model so .parsed.{self.field_name} is unambiguous."
            )
        if not self.field_name:
            raise TypeError(
                f"FieldGate for {self.document_type.__name__} must declare a non-empty field_name. "
                "Pass the exact model field to inspect, for example FieldGate(MyDecisionDoc, 'should_continue', op='falsy')."
            )
        if self.field_name not in content_type.model_fields:
            available_fields = ", ".join(sorted(content_type.model_fields)) or "(none)"
            raise TypeError(
                f"FieldGate field '{self.field_name}' is not defined on {self.document_type.__name__}.parsed ({content_type.__name__}). "
                f"Available fields: {available_fields}."
            )
        if self.op in {"eq", "ne"} and self.value is None:
            raise TypeError(
                f"FieldGate op='{self.op}' requires a comparison value. Pass value=... when comparing {self.document_type.__name__}.parsed.{self.field_name}."
            )
        if self.op in {"truthy", "falsy"} and self.value is not None:
            raise TypeError(
                f"FieldGate op='{self.op}' does not use value=. "
                f"Remove value=... or switch to op='eq'/'ne' for {self.document_type.__name__}.parsed.{self.field_name}."
            )


@dataclass(frozen=True, slots=True)
class FlowStep:
    """A single step in a deployment execution plan."""

    flow: PipelineFlow
    run_if: FieldGate | None = None
    group: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.flow, PipelineFlow):
            raise TypeError(
                f"FlowStep.flow must be a PipelineFlow instance, got {type(self.flow).__name__}. "
                "Instantiate the flow before putting it in DeploymentPlan(steps=...)."
            )
        if self.group is not None and not self.group:
            raise TypeError("FlowStep.group must be a non-empty string or None.")


@dataclass(frozen=True, slots=True)
class DeploymentPlan:
    """Complete static execution plan for a deployment run."""

    steps: tuple[FlowStep, ...]
    group_stop_if: Mapping[str, FieldGate] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        normalized_steps = tuple(self.steps)
        if not normalized_steps:
            raise ValueError("DeploymentPlan.steps must contain at least one FlowStep.")
        if any(not isinstance(step, FlowStep) for step in normalized_steps):
            bad_type = next(type(step).__name__ for step in normalized_steps if not isinstance(step, FlowStep))
            raise TypeError(f"DeploymentPlan.steps must contain only FlowStep instances, got {bad_type}. Wrap each flow as FlowStep(MyFlow()).")
        object.__setattr__(self, "steps", normalized_steps)

        raw_group_stop_if = self.group_stop_if
        normalized_group_stop_if = dict(raw_group_stop_if.items())
        for group_name, gate in normalized_group_stop_if.items():
            if not group_name:
                raise TypeError("DeploymentPlan.group_stop_if keys must be non-empty group names.")
            if not isinstance(gate, FieldGate):
                raise TypeError(f"DeploymentPlan.group_stop_if['{group_name}'] must be a FieldGate, got {type(gate).__name__}.")
        object.__setattr__(self, "group_stop_if", MappingProxyType(normalized_group_stop_if))


@dataclass(frozen=True, slots=True)
class FlowOutputs:
    """Typed accessor for deployment result assembly."""

    documents: tuple[Document[Any], ...]

    def __init__(self, documents: Sequence[Document[Any]]) -> None:
        object.__setattr__(self, "documents", tuple(documents))

    def latest(self, document_type: type[TDocument]) -> TDocument | None:
        """Return the latest document of ``document_type``, or None."""
        for document in reversed(self.documents):
            if isinstance(document, document_type):
                return document
        return None

    def all(self, document_type: type[TDocument]) -> tuple[TDocument, ...]:
        """Return all documents of ``document_type`` in accumulation order."""
        return tuple(document for document in self.documents if isinstance(document, document_type))


# Enum
class EventType(StrEnum):
    """Pipeline lifecycle event types for Pub/Sub publishing."""

    RUN_STARTED = "run.started"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_HEARTBEAT = "run.heartbeat"
    FLOW_STARTED = "flow.started"
    FLOW_COMPLETED = "flow.completed"
    FLOW_FAILED = "flow.failed"
    FLOW_SKIPPED = "flow.skipped"
    TASK_STARTED = "task.started"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"


# Enum
class ErrorCode(StrEnum):
    """Classifies pipeline failure reason for run.failed events."""

    BUDGET_EXCEEDED = "budget_exceeded"
    DURATION_EXCEEDED = "duration_exceeded"
    PROVIDER_ERROR = "provider_error"
    PIPELINE_ERROR = "pipeline_error"
    INVALID_INPUT = "invalid_input"
    CRASHED = "crashed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RunStartedEvent:
    """Pipeline execution started."""

    run_id: str
    span_id: str
    root_deployment_id: str
    parent_deployment_task_id: str | None
    input_fingerprint: str
    status: str
    deployment_name: str = ""
    deployment_class: str = ""
    flow_plan: list[dict[str, Any]] = field(default_factory=list)
    parent_span_id: str = ""
    input_document_sha256s: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RunCompletedEvent:
    """Pipeline completed successfully.

    output_document_sha256s contains the SHA256 hashes of the LAST flow's
    output documents — these are the pipeline's final deliverables. Intermediate
    documents from earlier flows are available via the database (ai-trace show).
    """

    run_id: str
    span_id: str
    root_deployment_id: str
    parent_deployment_task_id: str | None
    status: str
    result: dict[str, Any]
    deployment_name: str = ""
    deployment_class: str = ""
    duration_ms: int = 0
    output_document_sha256s: tuple[str, ...] = ()
    parent_span_id: str = ""


@dataclass(frozen=True, slots=True)
class RunFailedEvent:
    """Pipeline execution failed."""

    run_id: str
    span_id: str
    root_deployment_id: str
    parent_deployment_task_id: str | None
    status: str
    error_code: ErrorCode
    error_message: str
    deployment_name: str = ""
    deployment_class: str = ""
    duration_ms: int = 0
    parent_span_id: str = ""


@dataclass(frozen=True, slots=True)
class FlowStartedEvent:
    """Flow execution started."""

    run_id: str
    span_id: str
    root_deployment_id: str
    parent_deployment_task_id: str | None
    flow_name: str
    flow_class: str
    step: int
    total_steps: int
    status: str
    expected_tasks: list[dict[str, Any]] = field(default_factory=list)
    flow_params: dict[str, Any] = field(default_factory=dict)
    parent_span_id: str = ""
    input_document_sha256s: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FlowCompletedEvent:
    """Flow execution completed."""

    run_id: str
    span_id: str
    root_deployment_id: str
    parent_deployment_task_id: str | None
    flow_name: str
    flow_class: str
    step: int
    total_steps: int
    status: str
    duration_ms: int
    output_documents: tuple[DocumentRef, ...] = field(default_factory=tuple)
    parent_span_id: str = ""
    input_document_sha256s: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FlowFailedEvent:
    """Flow execution failed."""

    run_id: str
    span_id: str
    root_deployment_id: str
    parent_deployment_task_id: str | None
    flow_name: str
    flow_class: str
    step: int
    total_steps: int
    status: str
    error_message: str
    parent_span_id: str = ""
    input_document_sha256s: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FlowSkippedEvent:
    """Flow skipped because it was resumed or intentionally bypassed."""

    run_id: str
    span_id: str
    root_deployment_id: str
    parent_deployment_task_id: str | None
    flow_name: str
    flow_class: str
    step: int
    total_steps: int
    status: str
    reason: str
    parent_span_id: str = ""
    input_document_sha256s: tuple[str, ...] = ()


# Protocol
@runtime_checkable
class ResultPublisher(Protocol):
    """Publishes pipeline lifecycle events to external consumers."""

    async def publish_run_started(self, event: RunStartedEvent) -> None:
        """Publish a pipeline start event."""
        ...

    async def publish_run_completed(self, event: RunCompletedEvent) -> None:
        """Publish a pipeline completion event."""
        ...

    async def publish_run_failed(self, event: RunFailedEvent) -> None:
        """Publish a pipeline failure event."""
        ...

    async def publish_heartbeat(self, run_id: str, *, root_deployment_id: str, span_id: str) -> None:
        """Publish a heartbeat signal."""
        ...

    async def publish_flow_started(self, event: FlowStartedEvent) -> None:
        """Publish a flow start event."""
        ...

    async def publish_flow_completed(self, event: FlowCompletedEvent) -> None:
        """Publish a flow completion event."""
        ...

    async def publish_flow_failed(self, event: FlowFailedEvent) -> None:
        """Publish a flow failure event."""
        ...

    async def publish_flow_skipped(self, event: FlowSkippedEvent) -> None:
        """Publish a flow skipped event."""
        ...

    async def publish_task_started(self, event: TaskStartedEvent) -> None:
        """Publish a task start event."""
        ...

    async def publish_task_completed(self, event: TaskCompletedEvent) -> None:
        """Publish a task completion event."""
        ...

    async def publish_task_failed(self, event: TaskFailedEvent) -> None:
        """Publish a task failure event."""
        ...

    async def close(self) -> None:
        """Release resources held by the publisher."""
        ...


class _NoopPublisher:
    """Discards all lifecycle events. Default publisher for CLI and run_local."""

    async def publish_run_started(self, event: RunStartedEvent) -> None:
        """Accept and discard a run started event."""

    async def publish_run_completed(self, event: RunCompletedEvent) -> None:
        """Accept and discard a run completed event."""

    async def publish_run_failed(self, event: RunFailedEvent) -> None:
        """Accept and discard a run failed event."""

    async def publish_heartbeat(self, run_id: str, *, root_deployment_id: str, span_id: str) -> None:
        """Accept and discard a heartbeat."""

    async def publish_flow_started(self, event: FlowStartedEvent) -> None:
        """Accept and discard a flow started event."""

    async def publish_flow_completed(self, event: FlowCompletedEvent) -> None:
        """Accept and discard a flow completed event."""

    async def publish_flow_failed(self, event: FlowFailedEvent) -> None:
        """Accept and discard a flow failed event."""

    async def publish_flow_skipped(self, event: FlowSkippedEvent) -> None:
        """Accept and discard a flow skipped event."""

    async def publish_task_started(self, event: TaskStartedEvent) -> None:
        """Accept and discard a task started event."""

    async def publish_task_completed(self, event: TaskCompletedEvent) -> None:
        """Accept and discard a task completed event."""

    async def publish_task_failed(self, event: TaskFailedEvent) -> None:
        """Accept and discard a task failed event."""

    async def close(self) -> None:
        """No resources to release."""


class _MemoryPublisher:
    """Records all lifecycle events in-memory for test assertions."""

    def __init__(self) -> None:
        self.events: list[
            RunStartedEvent
            | RunCompletedEvent
            | RunFailedEvent
            | FlowStartedEvent
            | FlowCompletedEvent
            | FlowFailedEvent
            | FlowSkippedEvent
            | TaskStartedEvent
            | TaskCompletedEvent
            | TaskFailedEvent
        ] = []
        self.heartbeats: list[dict[str, str]] = []

    async def publish_run_started(self, event: RunStartedEvent) -> None:
        """Record a run started event."""
        self.events.append(event)

    async def publish_run_completed(self, event: RunCompletedEvent) -> None:
        """Record a run completed event."""
        self.events.append(event)

    async def publish_run_failed(self, event: RunFailedEvent) -> None:
        """Record a run failed event."""
        self.events.append(event)

    async def publish_heartbeat(self, run_id: str, *, root_deployment_id: str, span_id: str) -> None:
        """Record a heartbeat."""
        self.heartbeats.append({"run_id": run_id, "root_deployment_id": root_deployment_id, "span_id": span_id})

    async def publish_flow_started(self, event: FlowStartedEvent) -> None:
        """Record a flow started event."""
        self.events.append(event)

    async def publish_flow_completed(self, event: FlowCompletedEvent) -> None:
        """Record a flow completed event."""
        self.events.append(event)

    async def publish_flow_failed(self, event: FlowFailedEvent) -> None:
        """Record a flow failed event."""
        self.events.append(event)

    async def publish_flow_skipped(self, event: FlowSkippedEvent) -> None:
        """Record a flow skipped event."""
        self.events.append(event)

    async def publish_task_started(self, event: TaskStartedEvent) -> None:
        """Record a task started event."""
        self.events.append(event)

    async def publish_task_completed(self, event: TaskCompletedEvent) -> None:
        """Record a task completed event."""
        self.events.append(event)

    async def publish_task_failed(self, event: TaskFailedEvent) -> None:
        """Record a task failed event."""
        self.events.append(event)

    async def close(self) -> None:
        """No resources to release."""


__all__ = [
    "DeploymentPlan",
    "DocumentRef",
    "ErrorCode",
    "EventType",
    "FieldGate",
    "FlowCompletedEvent",
    "FlowFailedEvent",
    "FlowOutputs",
    "FlowSkippedEvent",
    "FlowStartedEvent",
    "FlowStep",
    "ResultPublisher",
    "RunCompletedEvent",
    "RunFailedEvent",
    "RunStartedEvent",
    "TaskCompletedEvent",
    "TaskFailedEvent",
    "TaskStartedEvent",
    "_MemoryPublisher",
    "_NoopPublisher",
]
