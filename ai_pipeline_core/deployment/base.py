"""Core classes for pipeline deployments.

Provides the PipelineDeployment base class and related types for
creating unified, type-safe pipeline deployments with:
- Per-flow resume (skip completed flows via execution DAG)
- Per-flow uploads (immediate, not just at end)
- Upload on failure (partial results saved)
"""

import asyncio
import contextlib
import logging
import time as _time_mod
from abc import abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar, Generic, Literal, TypeVar, cast, final
from uuid import UUID, uuid7

from prefect import get_client, runtime
from prefect.logging import disable_run_logger
from prefect.testing.utilities import prefect_test_harness
from pydantic import BaseModel, ConfigDict

from ai_pipeline_core.database import CostTotals, SpanKind, SpanStatus
from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.documents import Document
from ai_pipeline_core.logger._buffer import ExecutionLogBuffer
from ai_pipeline_core.pipeline._execution_context import (
    ExecutionContext,
    FlowFrame,
    _RunContext,
    get_execution_context,
    get_sinks,
    record_lifecycle_event,
    set_execution_context,
    set_run_context,
)
from ai_pipeline_core.pipeline._flow import PipelineFlow
from ai_pipeline_core.pipeline._parallel import TaskHandle
from ai_pipeline_core.pipeline._runtime_sinks import build_runtime_sinks
from ai_pipeline_core.pipeline._track_span import track_span
from ai_pipeline_core.pipeline.limits import (
    PipelineLimit,
    _ensure_concurrency_limits,
    _LimitsState,
    _set_limits_state,
    _SharedStatus,
    _validate_concurrency_limits,
)
from ai_pipeline_core.pipeline.options import FlowOptions
from ai_pipeline_core.settings import settings

from ._cli import run_cli_for_deployment
from ._deployment_runtime import (
    _deduplicate_documents_by_sha256,
    _documents_from_flow_arguments,
    _execute_flow_with_context,
    _first_declaring_class,
    _resolve_flow_arguments,
    _reuse_cached_flow_output,
    _safe_uuid,
    _validate_flow_chain,
)
from ._helpers import (
    _build_flow_cache_key,
    _cancel_dispatched_handles,
    _classify_error,
    _compute_input_fingerprint,
    _create_span_database_from_settings,
    _ensure_execution_log_handler_installed,
    _heartbeat_loop,
    _log_flush_loop,
    build_auto_run_id,
    class_name_to_deployment_name,
    extract_generic_params,
    validate_run_id,
)
from ._prefect import build_prefect_flow
from ._types import (
    DeploymentPlan,
    FieldGate,
    FlowOutputs,
    FlowSkippedEvent,
    FlowStep,
    ResultPublisher,
    RunCompletedEvent,
    RunFailedEvent,
    RunStartedEvent,
    _NoopPublisher,
)

logger = logging.getLogger(__name__)


class DeploymentResult(BaseModel):
    """Base class for deployment results."""

    success: bool
    error: str | None = None

    model_config = ConfigDict(frozen=True)


TOptions = TypeVar("TOptions", bound=FlowOptions, default=FlowOptions)
TResult = TypeVar("TResult", bound=DeploymentResult, default=DeploymentResult)

_LABEL_RUN_ID = "pipeline.run_id"


async def _record_nonexecuted_flow(
    *,
    flow_instance: PipelineFlow,
    flow_class: type[PipelineFlow],
    flow_name: str,
    available_docs: list[Document],
    resolved_kwargs: dict[str, Any],
    flow_options_payload: dict[str, Any],
    expected_tasks: list[dict[str, Any]],
    step: int,
    total_steps: int,
    status: SpanStatus,
    publish_reason: str,
    lifecycle_event: str,
    lifecycle_message: str,
    lifecycle_fields: dict[str, Any],
    publisher: ResultPublisher,
    run_id: str,
    root_deployment_id: str,
    parent_deployment_task_id: str | None,
    deployment_span_id: UUID,
    database: Any,
    cache_key: str = "",
    cache_source_span_id: str = "",
    output_documents: tuple[Document, ...] = (),
) -> None:
    input_documents = _documents_from_flow_arguments(resolved_kwargs)
    flow_target = f"instance_method:{flow_class.__module__}:{flow_class.__qualname__}.run"
    async with track_span(
        kind=SpanKind.FLOW,
        name=flow_name,
        target=flow_target,
        sinks=get_sinks(),
        parent_span_id=deployment_span_id,
        encode_receiver={"mode": "constructor_args", "value": flow_instance.get_params()},
        encode_input=resolved_kwargs,
        db=database,
        input_preview={
            "flow_class": flow_class.__name__,
            "flow_options": flow_options_payload,
            "input_documents": [document.name for document in input_documents],
            "input_parameters": tuple(resolved_kwargs),
            "available_documents": [document.name for document in available_docs],
        },
    ) as flow_span_ctx:
        flow_span_ctx.set_status(status)
        flow_span_ctx.set_meta(
            step=step,
            total_steps=total_steps,
            estimated_minutes=flow_instance.estimated_minutes,
            expected_tasks=expected_tasks,
            cache_hit=status == SpanStatus.CACHED,
            cache_key=cache_key,
            cache_source_span_id=cache_source_span_id,
            skip_reason=publish_reason,
        )
        await publisher.publish_flow_skipped(
            FlowSkippedEvent(
                run_id=run_id,
                span_id=str(flow_span_ctx.span_id),
                root_deployment_id=root_deployment_id,
                parent_deployment_task_id=parent_deployment_task_id,
                flow_name=flow_name,
                flow_class=flow_class.__name__,
                step=step,
                total_steps=total_steps,
                status=str(status),
                reason=publish_reason,
                parent_span_id=str(deployment_span_id),
                input_document_sha256s=tuple(doc.sha256 for doc in input_documents),
            )
        )
        record_lifecycle_event(lifecycle_event, lifecycle_message, **lifecycle_fields)
        if output_documents:
            flow_span_ctx.set_output_preview({"documents": [document.name for document in output_documents]})
            flow_span_ctx._set_output_value(output_documents)


async def _load_deployment_cost_totals(
    *,
    database: Any,
    root_deployment_id: UUID,
    deployment_span_id: UUID,
) -> CostTotals:
    if database is None:
        return CostTotals()
    try:
        return await database.get_deployment_cost_totals(root_deployment_id)
    except Exception as exc:
        logger.warning("Deployment cost totals query failed for %s: %s", deployment_span_id, exc)
        return CostTotals()


_MISSING_FIELD_VALUE = object()


def _missing_gate_result(gate: FieldGate, *, context: Literal["run", "stop"]) -> bool:
    """Resolve FieldGate.on_missing semantics for run vs stop contexts."""
    if context == "run":
        return gate.on_missing == "run"
    return gate.on_missing == "skip"


def _evaluate_field_gate(
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
        except Exception as exc:
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


def _gate_reason(gate: FieldGate) -> str:
    if gate.op in {"eq", "ne"}:
        return f"{gate.document_type.__name__}.{gate.field_name} {gate.op} {gate.value!r}"
    return f"{gate.document_type.__name__}.{gate.field_name} {gate.op}"


def _apply_group_stop_gates(plan: DeploymentPlan, accumulated_docs: list[Document], stopped_groups: set[str]) -> None:
    """Mark groups as stopped when their group_stop_if gate passes."""
    for group_name, stop_gate in plan.group_stop_if.items():
        if group_name in stopped_groups:
            continue
        if _evaluate_field_gate(stop_gate, accumulated_docs, context="stop"):
            stopped_groups.add(group_name)


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


def _warn_on_unused_flow_outputs(deployment_name: str, plan: DeploymentPlan) -> None:
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


class PipelineDeployment(Generic[TOptions, TResult]):
    """Base class for pipeline deployments with three execution modes.

    - ``run_cli()``: Database-backed (ClickHouse or filesystem)
    - ``run_local()``: In-memory database (ephemeral)
    - ``as_prefect_flow()``: auto-configured from settings
    """

    name: ClassVar[str]
    options_type: ClassVar[type[FlowOptions]]
    result_type: ClassVar[type[DeploymentResult]]
    # Sets CloudEvents ``source`` attribute (e.g. ``ai-{service_type}-worker``).
    # Does not affect topic routing. Requires PUBSUB_PROJECT_ID + PUBSUB_TOPIC_ID. Empty = _NoopPublisher.
    pubsub_service_type: ClassVar[str] = ""
    cache_ttl: ClassVar[timedelta | None] = timedelta(hours=24)
    flow_retries: ClassVar[int | None] = None
    flow_retry_delay_seconds: ClassVar[int | None] = None
    concurrency_limits: ClassVar[Mapping[str, PipelineLimit]] = MappingProxyType({})

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)

        if cls.__name__.startswith("Test"):
            raise TypeError(f"Deployment class name cannot start with 'Test': {cls.__name__}")

        if "name" not in cls.__dict__:
            cls.name = class_name_to_deployment_name(cls.__name__)

        generic_args = extract_generic_params(cls, PipelineDeployment)
        if len(generic_args) < 2:
            raise TypeError(f"{cls.__name__} must specify Generic parameters: class {cls.__name__}(PipelineDeployment[MyOptions, MyResult])")
        options_type, result_type = generic_args[0], generic_args[1]

        cls.options_type = options_type
        cls.result_type = result_type

        # build_result must be implemented (not still abstract from PipelineDeployment)
        build_result_fn = getattr(cls, "build_result", None)
        if build_result_fn is None or getattr(build_result_fn, "__isabstractmethod__", False):
            raise TypeError(f"{cls.__name__} must implement 'build_result' static method")

        flows_declaring_class = _first_declaring_class(cls, "build_flows")
        plan_declaring_class = _first_declaring_class(cls, "build_plan")
        if flows_declaring_class is PipelineDeployment and plan_declaring_class is PipelineDeployment:
            raise TypeError(
                f"{cls.__name__} must implement either build_flows(options) -> Sequence[PipelineFlow] "
                f"or build_plan(options) -> DeploymentPlan. Decorator-based `flows = [...]` is removed."
            )

        # Concurrency limits validation
        cls.concurrency_limits = _validate_concurrency_limits(cls.__name__, getattr(cls, "concurrency_limits", MappingProxyType({})))

    def build_flows(self, options: TOptions) -> Sequence[PipelineFlow]:
        """Build flow instances for this run."""
        raise NotImplementedError(f"{type(self).__name__}.build_flows() must return a sequence of PipelineFlow.")

    def build_plan(self, options: TOptions) -> DeploymentPlan:
        """Build the deployment execution plan for this run."""
        flows = tuple(self.build_flows(options))
        if not flows:
            raise ValueError(f"{type(self).__name__}.build_flows() returned an empty list. Provide at least one PipelineFlow.")
        for flow_item in cast(tuple[Any, ...], flows):
            if not isinstance(flow_item, PipelineFlow):
                raise TypeError(f"{type(self).__name__}.build_flows() must return PipelineFlow instances, got {type(flow_item).__name__}.")
        return DeploymentPlan(steps=tuple(FlowStep(flow=flow_instance) for flow_instance in flows))

    @staticmethod
    @abstractmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: TOptions) -> TResult:
        """Extract typed result from pipeline documents.

        Called for both full runs and partial runs (--start/--end). For partial runs,
        build_partial_result() delegates here by default — override build_partial_result()
        to customize partial run results.
        """
        ...

    def build_partial_result(self, run_id: str, documents: tuple[Document, ...], options: TOptions) -> TResult:
        """Build a result for partial pipeline runs (--start/--end that don't reach the last step).

        Override this method to customize partial run results. Default delegates to build_result.
        """
        return self.build_result(run_id, documents, options)

    def _all_document_types(self, flows: Sequence[PipelineFlow]) -> list[type[Document]]:
        """Collect all document types from all flows (inputs + outputs), deduplicated."""
        types: dict[str, type[Document]] = {}
        for flow_inst in flows:
            flow_cls = type(flow_inst)
            for t in flow_cls.input_document_types:
                types[t.__name__] = t
            for t in flow_cls.output_document_types:
                types[t.__name__] = t
        return list(types.values())

    @staticmethod
    async def _shutdown_db(database: Any) -> None:
        """Flush and shut down database, logging warnings on failure."""
        if database is None:
            return
        try:
            await database.flush()
        except Exception as exc:
            logger.warning("Database flush failed: %s", exc)
        try:
            await database.shutdown()
        except Exception as exc:
            logger.warning("Database shutdown failed: %s", exc)

    @final
    async def _run_with_context(
        self,
        run_id: str,
        documents: Sequence[Document],
        options: TOptions,
        *,
        deployment_span_id: UUID | None = None,
        root_deployment_id: UUID | None = None,
        parent_deployment_task_id: UUID | None = None,
        remote_child_run_id: str | None = None,
        publisher: ResultPublisher | None = None,
        start_step: int = 1,
        end_step: int | None = None,
        parent_execution_id: UUID | None = None,
        database: Any = None,
    ) -> TResult:
        """Internal entry point with pre-allocated DAG-linking parameters.

        Called by public run() (standalone), remote deployment (Prefect), and inline mode.
        """
        resolved_deployment_span_id = deployment_span_id or uuid7()
        resolved_root_deployment_id = root_deployment_id or resolved_deployment_span_id
        return await self._run_core(
            run_id=run_id,
            documents=documents,
            options=options,
            publisher=publisher,
            start_step=start_step,
            end_step=end_step,
            parent_execution_id=parent_execution_id,
            deployment_span_id=resolved_deployment_span_id,
            root_deployment_id=resolved_root_deployment_id,
            parent_deployment_task_id=parent_deployment_task_id,
            remote_child_run_id=remote_child_run_id,
            database=database,
        )

    @final
    async def run(
        self,
        run_id: str,
        documents: Sequence[Document],
        options: TOptions,
        publisher: ResultPublisher | None = None,
        start_step: int = 1,
        end_step: int | None = None,
        parent_execution_id: UUID | None = None,
        database: Any = None,
    ) -> TResult:
        """Execute flows with resume, per-flow uploads, and step control.

        run_id must match ``[a-zA-Z0-9_-]+``, max 100 chars.
        """
        return await self._run_with_context(
            run_id,
            documents,
            options,
            parent_deployment_task_id=None,
            publisher=publisher,
            start_step=start_step,
            end_step=end_step,
            parent_execution_id=parent_execution_id,
            database=database,
        )

    async def _run_core(
        self,
        run_id: str,
        documents: Sequence[Document],
        options: TOptions,
        *,
        deployment_span_id: UUID,
        root_deployment_id: UUID,
        parent_deployment_task_id: UUID | None = None,
        remote_child_run_id: str | None = None,
        publisher: ResultPublisher | None = None,
        start_step: int = 1,
        end_step: int | None = None,
        parent_execution_id: UUID | None = None,
        database: Any = None,
    ) -> TResult:
        """Core deployment execution with append-only span tracking."""
        validate_run_id(run_id)

        if publisher is None:
            publisher = _NoopPublisher()
        plan = self.build_plan(options)
        if not isinstance(plan, DeploymentPlan):
            raise TypeError(
                f"{type(self).__name__}.build_plan() must return DeploymentPlan, got {type(plan).__name__}. "
                "Wrap each flow as FlowStep(...) and return DeploymentPlan(steps=...)."
            )
        flows = [step.flow for step in plan.steps]
        _validate_flow_chain(type(self).__name__, flows)
        _warn_on_unused_flow_outputs(type(self).__name__, plan)

        total_steps = len(plan.steps)

        if end_step is None:
            end_step = total_steps
        if start_step < 1 or start_step > total_steps:
            raise ValueError(f"start_step must be 1-{total_steps}, got {start_step}")
        if end_step < start_step or end_step > total_steps:
            raise ValueError(f"end_step must be {start_step}-{total_steps}, got {end_step}")

        flow_run_id: str = str(runtime.flow_run.get_id() or "") if runtime.flow_run else ""  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownArgumentType]

        # Write identity labels for polling endpoint
        flow_run_uuid = _safe_uuid(flow_run_id) if flow_run_id else None
        if flow_run_uuid is not None:
            try:
                async with get_client() as client:
                    await client.update_flow_run_labels(
                        flow_run_id=flow_run_uuid,
                        labels={_LABEL_RUN_ID: run_id},
                    )
            except Exception as e:
                logger.warning("Identity label update failed: %s", e)

        input_docs = list(documents)
        input_fingerprint = _compute_input_fingerprint(input_docs, options)
        flow_plan = [
            {
                "name": flow_step.flow.name,
                "flow_class": type(flow_step.flow).__name__,
                "step": idx + 1,
                "estimated_minutes": flow_step.flow.estimated_minutes,
                "params": flow_step.flow.get_params(),
                "expected_tasks": type(flow_step.flow).expected_tasks(),
            }
            for idx, flow_step in enumerate(plan.steps)
        ]

        # Common event fields for this deployment
        deployment_span_id_str = str(deployment_span_id)
        root_id_str = str(root_deployment_id)
        parent_task_id_str = str(parent_deployment_task_id) if parent_deployment_task_id else None

        # Create database backend if not provided externally
        owns_database = database is None
        if owns_database:
            try:
                database = _create_span_database_from_settings(settings)
            except Exception as exc:
                logger.warning("Database creation failed, continuing without execution span tracking: %s", exc)
                database = None

        log_buffer: ExecutionLogBuffer | None = None
        flush_event: asyncio.Event | None = None
        log_flush_task: asyncio.Task[None] | None = None
        if database is not None:
            _ensure_execution_log_handler_installed()
            flush_event = asyncio.Event()
            event_loop = asyncio.get_running_loop()

            def _request_log_flush() -> None:
                event_loop.call_soon_threadsafe(flush_event.set)

            log_buffer = ExecutionLogBuffer(
                request_flush=_request_log_flush,
            )

        # Set concurrency limits and run context for the entire pipeline run
        run_execution_id = uuid7()
        limits_status = _SharedStatus()
        run_scope = contextlib.ExitStack()
        run_scope.enter_context(_set_limits_state(_LimitsState(limits=self.concurrency_limits, status=limits_status)))
        run_scope.enter_context(set_run_context(_RunContext(run_id=run_id, execution_id=run_execution_id)))
        run_scope.enter_context(
            set_execution_context(
                ExecutionContext(
                    run_id=run_id,
                    execution_id=run_execution_id,
                    publisher=publisher,
                    limits=self.concurrency_limits,
                    limits_status=limits_status,
                    database=database,
                    cache_ttl=self.cache_ttl,
                    deployment_id=deployment_span_id,
                    root_deployment_id=root_deployment_id,
                    parent_deployment_task_id=parent_deployment_task_id,
                    deployment_name=self.name,
                    span_id=deployment_span_id,
                    current_span_id=deployment_span_id,
                    log_buffer=log_buffer,
                    sinks=build_runtime_sinks(database=database, settings_obj=settings),
                )
            )
        )
        try:
            if flush_event is not None:
                log_flush_task = asyncio.create_task(_log_flush_loop(database, log_buffer, flush_event))
            async with track_span(
                kind=SpanKind.DEPLOYMENT,
                name=self.name,
                target="",
                sinks=get_sinks(),
                span_id=deployment_span_id,
                parent_span_id=parent_deployment_task_id,
                encode_input={"documents": tuple(input_docs), "options": options},
                db=database,
                input_preview={"deployment": self.name, "document_count": len(input_docs)},
            ) as deployment_span_ctx:
                deployment_span_ctx.set_meta(
                    input_fingerprint=input_fingerprint,
                    flow_plan=flow_plan,
                    deployment_class=type(self).__name__,
                )
                return await self._run_tracked_deployment(
                    run_id=run_id,
                    publisher=publisher,
                    plan=plan,
                    options=options,
                    input_docs=input_docs,
                    input_fingerprint=input_fingerprint,
                    start_step=start_step,
                    end_step=end_step,
                    total_steps=total_steps,
                    database=database,
                    deployment_span_id=deployment_span_id,
                    root_deployment_id=root_deployment_id,
                    flow_plan=flow_plan,
                    deployment_span_id_str=deployment_span_id_str,
                    root_id_str=root_id_str,
                    parent_task_id_str=parent_task_id_str,
                    deployment_span_ctx=deployment_span_ctx,
                    flow_run_id=flow_run_id,
                )
        finally:
            if log_flush_task is not None:
                log_flush_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await log_flush_task
            if owns_database:
                await self._shutdown_db(database)
            run_scope.close()

    async def _run_tracked_deployment(
        self,
        *,
        run_id: str,
        publisher: ResultPublisher,
        plan: DeploymentPlan,
        options: TOptions,
        input_docs: list[Document],
        input_fingerprint: str,
        start_step: int,
        end_step: int,
        total_steps: int,
        database: Any,
        deployment_span_id: UUID,
        root_deployment_id: UUID,
        flow_plan: list[dict[str, Any]],
        deployment_span_id_str: str,
        root_id_str: str,
        parent_task_id_str: str | None,
        deployment_span_ctx: Any,
        flow_run_id: str,
    ) -> TResult:
        heartbeat_task: asyncio.Task[None] | None = None
        failed_published = False
        deployment_start_mono = _time_mod.monotonic()
        try:
            record_lifecycle_event(
                "deployment.started",
                "Starting deployment",
                deployment_name=self.name,
                total_steps=total_steps,
                start_step=start_step,
                end_step=end_step,
            )
            await publisher.publish_run_started(
                RunStartedEvent(
                    run_id=run_id,
                    span_id=deployment_span_id_str,
                    root_deployment_id=root_id_str,
                    parent_deployment_task_id=parent_task_id_str,
                    input_fingerprint=input_fingerprint,
                    status=str(SpanStatus.RUNNING),
                    deployment_name=self.name,
                    deployment_class=type(self).__name__,
                    flow_plan=flow_plan,
                    parent_span_id=parent_task_id_str or "",
                    input_document_sha256s=tuple(doc.sha256 for doc in input_docs),
                )
            )

            heartbeat_task = asyncio.create_task(_heartbeat_loop(publisher, run_id, root_deployment_id=root_id_str, span_id=deployment_span_id_str))
            await _ensure_concurrency_limits(self.concurrency_limits)

            flow_minutes = tuple(flow_step.flow.estimated_minutes for flow_step in plan.steps)
            accumulated_docs: list[Document] = list(_deduplicate_documents_by_sha256(input_docs))
            stopped_groups: set[str] = set()
            last_flow_output_sha256s: tuple[str, ...] = ()
            previous_output_documents: tuple[Document, ...] = ()
            _apply_group_stop_gates(plan, accumulated_docs, stopped_groups)

            for i in range(start_step - 1, end_step):
                step = i + 1
                flow_step = plan.steps[i]
                flow_instance = flow_step.flow
                flow_class = type(flow_instance)
                flow_name = flow_instance.name
                flow_run_id = str(runtime.flow_run.get_id() or "") if runtime.flow_run else flow_run_id  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownArgumentType]
                flow_params = flow_instance.get_params()
                expected_tasks = flow_class.expected_tasks()
                flow_options_payload = options.model_dump(mode="json")
                blackboard_before_flow = list(accumulated_docs)
                resolved_flow_kwargs = _resolve_flow_arguments(flow_class, blackboard_before_flow, options, allow_partial=True)

                if flow_step.group is not None and flow_step.group in stopped_groups:
                    previous_output_documents = ()
                    await _record_nonexecuted_flow(
                        flow_instance=flow_instance,
                        flow_class=flow_class,
                        flow_name=flow_name,
                        available_docs=blackboard_before_flow,
                        resolved_kwargs=resolved_flow_kwargs,
                        flow_options_payload=flow_options_payload,
                        expected_tasks=expected_tasks,
                        step=step,
                        total_steps=total_steps,
                        status=SpanStatus.SKIPPED,
                        publish_reason=f"group '{flow_step.group}' stopped",
                        lifecycle_event="flow.skipped",
                        lifecycle_message=f"Skipped flow {flow_name}",
                        lifecycle_fields={
                            "flow_name": flow_name,
                            "flow_class": flow_class.__name__,
                            "step": step,
                            "total_steps": total_steps,
                            "reason": f"group '{flow_step.group}' stopped",
                        },
                        publisher=publisher,
                        run_id=run_id,
                        root_deployment_id=root_id_str,
                        parent_deployment_task_id=parent_task_id_str,
                        deployment_span_id=deployment_span_id,
                        database=database,
                    )
                    continue

                if flow_step.run_if is not None and not _evaluate_field_gate(flow_step.run_if, accumulated_docs):
                    previous_output_documents = ()
                    await _record_nonexecuted_flow(
                        flow_instance=flow_instance,
                        flow_class=flow_class,
                        flow_name=flow_name,
                        available_docs=blackboard_before_flow,
                        resolved_kwargs=resolved_flow_kwargs,
                        flow_options_payload=flow_options_payload,
                        expected_tasks=expected_tasks,
                        step=step,
                        total_steps=total_steps,
                        status=SpanStatus.SKIPPED,
                        publish_reason=f"run_if gate did not pass: {_gate_reason(flow_step.run_if)}",
                        lifecycle_event="flow.skipped",
                        lifecycle_message=f"Skipped flow {flow_name}",
                        lifecycle_fields={
                            "flow_name": flow_name,
                            "flow_class": flow_class.__name__,
                            "step": step,
                            "total_steps": total_steps,
                            "reason": f"run_if gate did not pass: {_gate_reason(flow_step.run_if)}",
                        },
                        publisher=publisher,
                        run_id=run_id,
                        root_deployment_id=root_id_str,
                        parent_deployment_task_id=parent_task_id_str,
                        deployment_span_id=deployment_span_id,
                        database=database,
                    )
                    continue

                flow_cache_key = _build_flow_cache_key(
                    input_fingerprint=input_fingerprint,
                    flow_class=flow_class,
                    step=step,
                    flow_params=flow_params,
                )
                exec_ctx = get_execution_context()
                cache_disabled = exec_ctx is not None and exec_ctx.disable_cache
                cached_result = await _reuse_cached_flow_output(
                    database=database,
                    cache_ttl=self.cache_ttl,
                    flow_cache_key=flow_cache_key,
                    flow_class=flow_class,
                    flow_name=flow_name,
                    step=step,
                    total_steps=total_steps,
                    accumulated_docs=accumulated_docs,
                    disable_cache=cache_disabled,
                )
                if cached_result is not None:
                    cached_span, previous_output_documents, accumulated_docs = cached_result
                    last_flow_output_sha256s = cached_span.output_document_shas
                    _apply_group_stop_gates(plan, accumulated_docs, stopped_groups)
                    await _record_nonexecuted_flow(
                        flow_instance=flow_instance,
                        flow_class=flow_class,
                        flow_name=flow_name,
                        available_docs=blackboard_before_flow,
                        resolved_kwargs=resolved_flow_kwargs,
                        flow_options_payload=flow_options_payload,
                        expected_tasks=expected_tasks,
                        step=step,
                        total_steps=total_steps,
                        status=SpanStatus.CACHED,
                        publish_reason="cached_result_available",
                        lifecycle_event="flow.cached",
                        lifecycle_message=f"Reused cached flow output for {flow_name}",
                        lifecycle_fields={
                            "flow_name": flow_name,
                            "flow_class": flow_class.__name__,
                            "step": step,
                            "total_steps": total_steps,
                            "cache_key": flow_cache_key,
                            "cache_source_span_id": str(cached_span.span_id),
                        },
                        publisher=publisher,
                        run_id=run_id,
                        root_deployment_id=root_id_str,
                        parent_deployment_task_id=parent_task_id_str,
                        deployment_span_id=deployment_span_id,
                        database=database,
                        cache_key=flow_cache_key,
                        cache_source_span_id=str(cached_span.span_id),
                        output_documents=previous_output_documents,
                    )
                    continue

                flow_span_id = uuid7()
                logger.info("[%d/%d] Starting: %s", step, total_steps, flow_name)

                completed_mins = sum(flow_minutes[: max(step - 1, 0)])
                flow_frame = FlowFrame(
                    name=flow_name,
                    flow_class_name=flow_class.__name__,
                    step=step,
                    total_steps=total_steps,
                    flow_minutes=flow_minutes,
                    completed_minutes=completed_mins,
                    flow_params=flow_params,
                )
                current_exec_ctx = get_execution_context()
                flow_exec_ctx = (
                    replace(
                        current_exec_ctx,
                        flow_frame=flow_frame,
                        task_frame=None,
                        span_id=flow_span_id,
                        current_span_id=flow_span_id,
                        flow_span_id=flow_span_id,
                    )
                    if current_exec_ctx is not None
                    else None
                )
                active_handles_before: set[object] = set(current_exec_ctx.active_task_handles) if current_exec_ctx is not None else set()
                validated_docs = await _execute_flow_with_context(
                    flow_instance=flow_instance,
                    flow_class=flow_class,
                    flow_name=flow_name,
                    current_docs=blackboard_before_flow,
                    options=options,
                    flow_exec_ctx=flow_exec_ctx,
                    current_exec_ctx=current_exec_ctx,
                    active_handles_before=active_handles_before,
                    database=database,
                    publisher=publisher,
                    deployment_span_id=deployment_span_id,
                    run_id=run_id,
                    flow_span_id=flow_span_id,
                    flow_cache_key=flow_cache_key,
                    flow_options_payload=flow_options_payload,
                    expected_tasks=expected_tasks,
                    step=step,
                    total_steps=total_steps,
                    root_id_str=root_id_str,
                    parent_task_id_str=parent_task_id_str,
                    deployment_flow_retries=self.flow_retries,
                    deployment_flow_retry_delay_seconds=self.flow_retry_delay_seconds,
                )

                if current_exec_ctx is not None:
                    leaked: list[TaskHandle[tuple[Document[Any], ...]]] = [
                        handle
                        for handle in current_exec_ctx.active_task_handles
                        if handle not in active_handles_before and isinstance(handle, TaskHandle) and not handle.done
                    ]
                    if leaked:
                        logger.warning(
                            "PipelineFlow '%s' returned with %d un-awaited dispatched task(s). Cancelling to prevent post-flow writes.",
                            flow_class.__name__,
                            len(leaked),
                        )
                        await _cancel_dispatched_handles(
                            current_exec_ctx.active_task_handles,
                            baseline_handles=active_handles_before,
                        )

                last_flow_output_sha256s = tuple(document.sha256 for document in validated_docs)
                previous_output_documents = tuple(validated_docs)
                accumulated_docs = list(_deduplicate_documents_by_sha256([*accumulated_docs, *validated_docs]))
                _apply_group_stop_gates(plan, accumulated_docs, stopped_groups)

            all_docs = _deduplicate_documents_by_sha256(accumulated_docs)
            result = self._build_run_result(
                run_id=run_id,
                all_docs=all_docs,
                options=options,
                start_step=start_step,
                end_step=end_step,
                total_steps=total_steps,
            )
            cost_totals = await _load_deployment_cost_totals(
                database=database,
                root_deployment_id=root_deployment_id,
                deployment_span_id=deployment_span_id,
            )
            record_lifecycle_event(
                "deployment.completed",
                "Completed deployment",
                deployment_name=self.name,
                total_steps=total_steps,
                output_count=len(all_docs),
                partial_run=end_step < total_steps,
                cost_usd=cost_totals.cost_usd,
            )
            await publisher.publish_run_completed(
                RunCompletedEvent(
                    run_id=run_id,
                    span_id=deployment_span_id_str,
                    root_deployment_id=root_id_str,
                    parent_deployment_task_id=parent_task_id_str,
                    status=str(SpanStatus.COMPLETED),
                    result=result.model_dump(),
                    deployment_name=self.name,
                    deployment_class=type(self).__name__,
                    duration_ms=int((_time_mod.monotonic() - deployment_start_mono) * 1000),
                    output_document_sha256s=last_flow_output_sha256s,
                    parent_span_id=parent_task_id_str or "",
                )
            )
            deployment_span_ctx.set_output_preview({"run_id": run_id, "result": result.model_dump(mode="json")})
            deployment_span_ctx._set_output_value({
                "result": result,
                "output_documents": previous_output_documents,
            })
            return result
        except (Exception, asyncio.CancelledError) as exc:
            record_lifecycle_event(
                "deployment.failed",
                "Deployment failed",
                deployment_name=self.name,
                total_steps=total_steps,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            current_exec_ctx = get_execution_context()
            if current_exec_ctx is not None:
                await _cancel_dispatched_handles(current_exec_ctx.active_task_handles, baseline_handles=set())
            if not failed_published:
                failed_published = True
                try:
                    error_code = _classify_error(exc)
                    deployment_span_ctx.set_meta(error_code=str(error_code))
                    await publisher.publish_run_failed(
                        RunFailedEvent(
                            run_id=run_id,
                            span_id=deployment_span_id_str,
                            root_deployment_id=root_id_str,
                            parent_deployment_task_id=parent_task_id_str,
                            status=str(SpanStatus.FAILED),
                            error_code=error_code,
                            error_message=str(exc),
                            deployment_name=self.name,
                            deployment_class=type(self).__name__,
                            duration_ms=int((_time_mod.monotonic() - deployment_start_mono) * 1000),
                            parent_span_id=parent_task_id_str or "",
                        )
                    )
                except Exception as pub_err:
                    logger.warning("Failed to publish failure event: %s", pub_err)
            raise
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task

    def _build_run_result(
        self,
        *,
        run_id: str,
        all_docs: Sequence[Document],
        options: TOptions,
        start_step: int,
        end_step: int,
        total_steps: int,
    ) -> TResult:
        is_partial_run = end_step < total_steps
        if is_partial_run:
            logger.info("Partial run (steps %d-%d of %d) — skipping build_result", start_step, end_step, total_steps)
            return self.build_partial_result(run_id, tuple(all_docs), options)
        return self.build_result(run_id, tuple(all_docs), options)

    @final
    def run_local(
        self,
        run_id: str | None,
        documents: Sequence[Document],
        options: TOptions,
        publisher: ResultPublisher | None = None,
        output_dir: Path | None = None,
    ) -> TResult:
        """Run locally with Prefect test harness and in-memory database.

        Args:
            run_id: Pipeline run identifier. If None, auto-generated from output_dir + date + input hash.
            documents: Initial input documents.
            options: Flow options.
            publisher: Optional lifecycle event publisher (defaults to _NoopPublisher).
            output_dir: Optional directory for writing result.json.

        Returns:
            Typed deployment result.
        """
        if run_id is None:
            dir_name = output_dir.name if output_dir else "local"
            run_id = build_auto_run_id(output_dir_name=dir_name, documents=documents, options=options)

        if output_dir:
            output_dir.mkdir(parents=True, exist_ok=True)

        with prefect_test_harness(), disable_run_logger():
            result = asyncio.run(self.run(run_id, documents, options, publisher=publisher, database=_MemoryDatabase()))

        if output_dir:
            (output_dir / "result.json").write_text(result.model_dump_json(indent=2))

        return result

    @final
    def run_cli(
        self,
        initializer: Callable[[TOptions], tuple[str, tuple[Document, ...]]] | None = None,
        cli_mixin: type | None = None,
    ) -> None:
        """Execute pipeline from CLI with positional working_directory and --start/--end flags."""
        run_cli_for_deployment(self, initializer, cli_mixin)

    @final
    def as_prefect_flow(self) -> Callable[..., Any]:
        """Generate a Prefect flow for production deployment via ``ai-pipeline-deploy`` CLI."""
        return build_prefect_flow(self)


__all__ = [
    "DeploymentPlan",
    "DeploymentResult",
    "FieldGate",
    "FlowOutputs",
    "FlowStep",
    "PipelineDeployment",
]
