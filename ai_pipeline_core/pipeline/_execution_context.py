"""Unified execution context for pipeline run, flow, and task scopes.

Replaces scattered ContextVars with a single shared context object that is
replaced at scope boundaries while intentionally sharing mutable runtime state
such as task handles and child-sequence counters across derived contexts.
"""

import importlib
import json
import logging
import math
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field, replace
from datetime import timedelta
from itertools import count
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid4

from ai_pipeline_core._execution_context_state import (
    get_execution_context_state,
    reset_execution_context_state,
    set_execution_context_state,
)
from ai_pipeline_core.database._protocol import DatabaseWriter
from ai_pipeline_core.documents import Document, DocumentSha256
from ai_pipeline_core.documents.utils import _is_document_sha256
from ai_pipeline_core.logger._buffer import ExecutionLogBuffer
from ai_pipeline_core.logger._handler import LogContext, reset_log_context, set_log_context
from ai_pipeline_core.pipeline._span_types import SpanSink
from ai_pipeline_core.pipeline.limits import PipelineLimit, _SharedStatus

__all__ = [
    "ExecutionContext",
    "FlowFrame",
    "ReplayExecutionContext",
    "TaskContext",
    "TaskFrame",
    "_RunContext",
    "_TaskDocumentContext",
    "add_cost",
    "assert_not_inside_task",
    "get_execution_context",
    "get_run_id",
    "get_sinks",
    "get_task_context",
    "pipeline_test_context",
    "record_lifecycle_event",
    "set_execution_context",
    "set_run_context",
    "set_task_context",
]

logger = logging.getLogger(__name__)

# --- Run-level context ---


@dataclass(frozen=True, slots=True)
class _RunContext:
    """Immutable context for a pipeline run, carried via ContextVar."""

    run_id: str
    execution_id: UUID | None = None


_run_context: ContextVar[_RunContext | None] = ContextVar("_run_context", default=None)


def set_run_context(ctx: _RunContext) -> Token[_RunContext | None]:
    """Set the run context for the current scope."""
    return _run_context.set(ctx)


# --- Task-level context ---


@dataclass
class TaskContext:
    """Task/flow execution scope tracked via ContextVar."""

    scope_kind: str = "task"
    task_class_name: str | None = None


_task_context: ContextVar[TaskContext | None] = ContextVar("_task_context", default=None)


def get_task_context() -> TaskContext | None:
    """Get the current task context, or None if not inside a pipeline task/flow."""
    return _task_context.get()


def set_task_context(ctx: TaskContext) -> Token[TaskContext | None]:
    """Set the task context for the current scope."""
    return _task_context.set(ctx)


def assert_not_inside_task(task_name: str) -> None:
    """Raise when a task is dispatched from inside another task body."""
    ctx = get_task_context()
    if ctx is not None and ctx.scope_kind == "task":
        raise RuntimeError(
            f"PipelineTask '{task_name}' cannot be called from inside another task ('{ctx.task_class_name}'). "
            "Tasks must be called from flows, not nested inside other tasks. "
            "Move the inner task call to the parent flow's run() method."
        )


# --- Task document lifecycle tracking ---


@dataclass
class _TaskDocumentContext:
    """Tracks document provenance validation within a single pipeline task or flow execution.

    Used by PipelineTask/PipelineFlow runtime to:
    - Validate that all derived_from/triggered_by SHA256 references point to known documents
    - Detect same-task interdependencies (doc B referencing doc A created in the same task)
    - Warn about documents with no provenance (no derived_from and no triggered_by)
    - Deduplicate returned documents by SHA256
    """

    output_sha256s: set[DocumentSha256] = field(default_factory=set)

    def validate_provenance(
        self,
        documents: list[Document],
        existing_sha256s: set[DocumentSha256],
    ) -> list[str]:
        """Validate provenance (derived_from and triggered_by) for returned documents.

        Checks:
        1. All SHA256 derived_from references exist in existing_sha256s.
        2. All triggered_by references exist in existing_sha256s.
        3. No same-task interdependencies: a returned document must not reference
           another document in the same output set.
        4. Documents with no derived_from AND no triggered_by get a warning (no provenance).

        Only SHA256-formatted entries in derived_from are validated; URLs and other reference
        strings are skipped.

        Returns a list of warning messages (empty if everything is valid).
        """
        self.output_sha256s = {doc.sha256 for doc in documents}
        warnings: list[str] = []

        for doc in documents:
            for src in doc.derived_from:
                if not _is_document_sha256(src):
                    continue
                if src in self.output_sha256s and src != doc.sha256:
                    warnings.append(f"Document '{doc.name}' references derived_from {src[:12]}... created in the same task (same-task interdependency)")
                elif src not in existing_sha256s and src not in self.output_sha256s:
                    warnings.append(f"Document '{doc.name}' references derived_from {src[:12]}... which does not exist in the store")

            for trigger in doc.triggered_by:
                if trigger in self.output_sha256s and trigger != doc.sha256:
                    warnings.append(f"Document '{doc.name}' references triggered_by {trigger[:12]}... created in the same task (same-task interdependency)")
                elif trigger not in existing_sha256s and trigger not in self.output_sha256s:
                    warnings.append(f"Document '{doc.name}' references triggered_by {trigger[:12]}... which does not exist in the store")

            if not doc.derived_from and not doc.triggered_by:
                warnings.append(f"Document '{doc.name}' has no derived_from and no triggered_by (no provenance)")

        return warnings

    @staticmethod
    def deduplicate(documents: list[Document]) -> list[Document]:
        """Deduplicate documents by SHA256, preserving first occurrence order."""
        seen: dict[DocumentSha256, Document] = {}
        for doc in documents:
            if doc.sha256 not in seen:
                seen[doc.sha256] = doc
        return list(seen.values())


def _create_noop_publisher() -> Any:
    publisher_type = importlib.import_module("ai_pipeline_core.deployment._types")._NoopPublisher
    return publisher_type()


@dataclass(slots=True)
class _RecordingState:
    degraded: bool = False
    replay_root_span_id: UUID | None = None
    persisted_document_shas: set[str] = field(default_factory=set)
    persisted_blob_shas: set[str] = field(default_factory=set)
    total_cost_usd: float = 0.0


@dataclass(frozen=True, slots=True)
class TaskFrame:
    """Identity of a task invocation in a nested task hierarchy."""

    task_class_name: str
    task_id: str
    depth: int
    parent: TaskFrame | None = None


@dataclass(frozen=True, slots=True)
class FlowFrame:
    """Flow execution state used for progress and task event enrichment."""

    name: str
    flow_class_name: str
    step: int
    total_steps: int
    flow_minutes: tuple[float, ...]
    completed_minutes: float
    flow_params: Mapping[str, Any]


@dataclass(slots=True)
class ExecutionContext:
    """Pipeline execution context propagated through async boundaries.

    The wrapper object is replaced as flow/task scopes change, but some nested
    mutable state is intentionally shared between derived contexts so child tasks
    and sequence counters stay coordinated across the whole run.
    """

    run_id: str
    execution_id: UUID | None
    publisher: Any
    limits: Mapping[str, PipelineLimit]
    limits_status: _SharedStatus
    flow_frame: FlowFrame | None = None
    task_frame: TaskFrame | None = None
    active_task_handles: set[object] = field(default_factory=set)

    database: DatabaseWriter | None = None
    cache_ttl: timedelta | None = None
    deployment_id: UUID | None = None
    root_deployment_id: UUID | None = None
    parent_deployment_task_id: UUID | None = None
    deployment_name: str = ""
    span_id: UUID | None = None
    parent_span_id: UUID | None = None
    current_span_id: UUID | None = None
    flow_span_id: UUID | None = None
    log_buffer: ExecutionLogBuffer | None = None
    sinks: tuple[SpanSink, ...] = ()
    disable_cache: bool = False
    _recording_state: _RecordingState = field(default_factory=_RecordingState)
    _child_sequence_counters: dict[UUID, count[int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.root_deployment_id is None and self.deployment_id is not None:
            self.root_deployment_id = self.deployment_id
        if self.span_id is None:
            self.span_id = self.current_span_id
        if self.parent_span_id is None and self.current_span_id != self.span_id:
            self.parent_span_id = self.current_span_id

    def next_child_sequence(self, parent_span_id: UUID) -> int:
        """Return the next monotonic sequence number for children of the given parent span."""
        if parent_span_id not in self._child_sequence_counters:
            self._child_sequence_counters[parent_span_id] = count()
        return next(self._child_sequence_counters[parent_span_id])

    def with_flow(self, flow_frame: FlowFrame) -> ExecutionContext:
        """Return a copy with a new flow frame and cleared task frame."""
        return replace(self, flow_frame=flow_frame, task_frame=None)

    def with_task(self, task_frame: TaskFrame) -> ExecutionContext:
        """Return a copy with a new task frame."""
        return replace(self, task_frame=task_frame)

    def with_span(self, span_id: UUID, *, parent_span_id: UUID | None) -> ExecutionContext:
        """Return a copy with a new current span ID."""
        return replace(
            self,
            span_id=span_id,
            parent_span_id=parent_span_id,
            current_span_id=span_id,
        )

    @property
    def recording_degraded(self) -> bool:
        return self._recording_state.degraded

    @recording_degraded.setter
    def recording_degraded(self, value: bool) -> None:
        self._recording_state.degraded = value

    @property
    def replay_root_span_id(self) -> UUID | None:
        return self._recording_state.replay_root_span_id

    @replay_root_span_id.setter
    def replay_root_span_id(self, value: UUID | None) -> None:
        self._recording_state.replay_root_span_id = value

    @property
    def total_cost_usd(self) -> float:
        """Running total of all costs incurred during this execution. Updated when each span finishes."""
        return self._recording_state.total_cost_usd


@dataclass(slots=True)
class ReplayExecutionContext(ExecutionContext):
    """Execution context used for replay runs."""

    @classmethod
    def create(
        cls,
        *,
        source_span_id: UUID,
        database: DatabaseWriter | None,
        publisher: Any,
        sinks: tuple[SpanSink, ...],
    ) -> ReplayExecutionContext:
        root_deployment_id = uuid4()
        return cls(
            run_id=f"replay-{str(source_span_id)[:8]}-{uuid4()}",
            execution_id=None,
            publisher=publisher,
            limits=MappingProxyType({}),
            limits_status=_SharedStatus(),
            database=database,
            cache_ttl=None,
            deployment_id=root_deployment_id,
            root_deployment_id=root_deployment_id,
            sinks=sinks,
            disable_cache=True,
        )


def get_execution_context() -> ExecutionContext | None:
    """Get the current execution context."""
    raw_context = get_execution_context_state()
    return raw_context if isinstance(raw_context, ExecutionContext) else None


def get_run_id() -> str:
    """Return the current run ID from the active execution context."""
    ctx = get_execution_context()
    if ctx is None:
        msg = (
            "get_run_id() called outside execution context. "
            "This function is available inside PipelineFlow.run() and PipelineTask.run() "
            "during deployment execution. "
            "In tests, wrap your code with pipeline_test_context(run_id='...')."
        )
        raise RuntimeError(msg)
    return ctx.run_id


def _build_log_context(ctx: ExecutionContext) -> LogContext | None:
    """Build a LogContext from an ExecutionContext if all required fields are present."""
    if ctx.log_buffer is not None and ctx.current_span_id is not None and ctx.deployment_id is not None:
        return LogContext(log_buffer=ctx.log_buffer, span_id=ctx.current_span_id, deployment_id=ctx.deployment_id)
    return None


@contextmanager
def set_execution_context(ctx: ExecutionContext) -> Generator[ExecutionContext]:
    """Set the execution context for the current scope.

    Yields:
        ExecutionContext: The execution context bound for the active scope.
    """
    context_token = set_execution_context_state(ctx)
    log_context_token = set_log_context(_build_log_context(ctx))
    try:
        yield ctx
    finally:
        reset_log_context(log_context_token)
        reset_execution_context_state(context_token)


def get_sinks() -> tuple[SpanSink, ...]:
    """Get the active span sinks from the current execution context."""
    execution_ctx = get_execution_context()
    if execution_ctx is None:
        return ()
    return execution_ctx.sinks


def record_lifecycle_event(event_type: str, message: str, **fields: Any) -> None:
    """Emit a structured lifecycle log event for the current execution scope."""
    logger.info(
        message,
        extra={
            "lifecycle": True,
            "event_type": event_type,
            "fields_json": json.dumps(fields, default=str, sort_keys=True),
        },
    )


@contextmanager
def pipeline_test_context(
    run_id: str = "test-run",
    publisher: Any | None = None,
    cache_ttl: timedelta | None = None,
) -> Generator[ExecutionContext]:
    """Set up an execution + task context for tests without full deployment wiring.

    Yields:
        The active execution context for the test scope.
    """
    deployment_id = uuid4()
    ctx = ExecutionContext(
        run_id=run_id,
        execution_id=None,
        publisher=publisher or _create_noop_publisher(),
        limits=MappingProxyType({}),
        limits_status=_SharedStatus(),
        cache_ttl=cache_ttl,
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        deployment_name="pipeline_test_context",
        span_id=deployment_id,
        current_span_id=deployment_id,
    )
    with set_execution_context(ctx), set_task_context(TaskContext(scope_kind="test", task_class_name="pipeline_test_context")):
        yield ctx


def add_cost(amount: float, reason: str = "") -> None:
    """Attach a cost to the current execution span.

    Accumulates across multiple calls within the same span. No-op outside
    execution context or when no span is active.

    Args:
        amount: Cost in USD. Must be non-negative and finite.
        reason: Reserved for future use. Currently discarded.
    """
    _ = reason
    if not math.isfinite(amount):
        raise ValueError(f"add_cost() amount must be a finite USD value, got {amount!r}. Pass the actual cost as a positive float (e.g. 0.006).")
    if amount < 0:
        raise ValueError(f"add_cost() amount must be non-negative, got {amount}. Pass the actual cost as a positive float (e.g. 0.006).")
    if amount <= 0:
        return
    _apply_cost_to_current_span(amount)


def _apply_cost_to_current_span(amount: float) -> None:
    from ai_pipeline_core.pipeline._track_span import get_current_span_context  # noqa: PLC0415  # circular: _track_span imports from this module

    span_ctx = get_current_span_context()
    if span_ctx is not None:
        span_ctx._add_cost(amount)
