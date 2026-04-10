"""Private helpers for deployment execution and flow validation."""

import asyncio
import contextlib
import inspect
import logging
import time
from collections.abc import Sequence
from datetime import timedelta
from typing import Any, cast, get_origin
from uuid import UUID, uuid7

from prefect.context import FlowRunContext as _FlowRunContext

from ai_pipeline_core._base_exceptions import NonRetriableError, StubNotImplementedError
from ai_pipeline_core.database import SpanKind, SpanRecord, SpanStatus
from ai_pipeline_core.database._documents import load_documents_from_database
from ai_pipeline_core.documents import Document
from ai_pipeline_core.pipeline._execution_context import (
    ExecutionContext,
    TaskContext,
    get_sinks,
    record_lifecycle_event,
    set_execution_context,
    set_task_context,
)
from ai_pipeline_core.pipeline._flow import PipelineFlow
from ai_pipeline_core.pipeline._span_types import SpanContext
from ai_pipeline_core.pipeline._track_span import track_span
from ai_pipeline_core.pipeline._type_validation import (
    _unwrap_annotated,
    collect_document_types,
    is_optional_annotation,
    resolve_type_hints,
    unwrap_optional,
)
from ai_pipeline_core.pipeline.options import FlowOptions
from ai_pipeline_core.settings import settings

from ._helpers import _cancel_dispatched_handles
from ._types import DocumentRef, FlowCompletedEvent, FlowFailedEvent, FlowStartedEvent, ResultPublisher

__all__ = [
    "_deduplicate_documents_by_sha256",
    "_execute_flow_with_context",
    "_execute_single_flow_attempt",
    "_first_declaring_class",
    "_flow_class_path",
    "_reuse_cached_flow_output",
    "_run_flow_with_retries",
    "_safe_uuid",
    "_validate_flow_chain",
]

logger = logging.getLogger(__name__)
_PUBLISH_EXCEPTIONS = (OSError, RuntimeError, TypeError, ValueError)
MAX_RETRY_DELAY_SECONDS = 300


def _flow_class_path(flow_class: type[PipelineFlow]) -> str:
    """Return the fully qualified flow class path recorded in span metadata."""
    return f"{flow_class.__module__}:{flow_class.__qualname__}"


async def _reuse_cached_flow_output(
    *,
    database: Any,
    cache_ttl: timedelta | None,
    flow_cache_key: str,
    flow_class: type[PipelineFlow],
    flow_name: str,
    step: int,
    total_steps: int,
    accumulated_docs: list[Document],
    disable_cache: bool = False,
) -> tuple[SpanRecord, tuple[Document, ...], list[Document]] | None:
    """Reuse cached flow outputs and return the cached span plus hydrated documents."""
    if database is None or cache_ttl is None or disable_cache:
        return None

    cached_span = await database.get_cached_completion(flow_cache_key, max_age=cache_ttl)
    if cached_span is None:
        return None

    logger.info("[%d/%d] Resume: skipping %s (completion record found)", step, total_steps, flow_name)

    previous_output_documents: tuple[Document, ...] = ()
    updated_docs = list(accumulated_docs)
    if cached_span.output_document_shas:
        expected_output_shas = tuple(cached_span.output_document_shas)
        resumed_docs = await load_documents_from_database(
            database,
            set(expected_output_shas),
            filter_types=list(flow_class.output_document_types) if flow_class.output_document_types else None,
        )
        resumed_docs_by_sha = {doc.sha256: doc for doc in resumed_docs}
        if len(resumed_docs_by_sha) != len(expected_output_shas) or any(sha not in resumed_docs_by_sha for sha in expected_output_shas):
            logger.warning(
                "[%d/%d] Resume: ignoring cached %s output because hydration was incomplete. "
                "Expected %d documents, hydrated %d. Re-running the flow to restore missing outputs.",
                step,
                total_steps,
                flow_name,
                len(expected_output_shas),
                len(resumed_docs_by_sha),
            )
            return None
        previous_output_documents = tuple(resumed_docs_by_sha[sha] for sha in expected_output_shas)
        updated_docs = list(_deduplicate_documents_by_sha256([*accumulated_docs, *previous_output_documents]))

    return cached_span, previous_output_documents, updated_docs


def _resolve_flow_retries(flow_class: type[PipelineFlow], deployment_flow_retries: int | None) -> int:
    """Resolve effective retry count: flow explicit override > deployment > Settings."""
    if "retries" in flow_class.__dict__ and flow_class.retries is not None:
        return flow_class.retries
    if deployment_flow_retries is not None:
        return deployment_flow_retries
    return settings.flow_retries


def _resolve_flow_retry_delay(flow_class: type[PipelineFlow], deployment_flow_retry_delay_seconds: int | None) -> int:
    """Resolve effective retry delay: flow explicit override > deployment > Settings."""
    if "retry_delay_seconds" in flow_class.__dict__ and flow_class.retry_delay_seconds is not None:
        return flow_class.retry_delay_seconds
    if deployment_flow_retry_delay_seconds is not None:
        return deployment_flow_retry_delay_seconds
    return settings.flow_retry_delay_seconds


async def _execute_single_flow_attempt(
    flow_class: type[PipelineFlow],
    flow_instance: PipelineFlow,
    resolved_kwargs: dict[str, Any],
) -> tuple[Document, ...]:
    """Run the flow once and validate the return value."""
    if flow_class._stub:
        raise StubNotImplementedError(
            f"PipelineFlow '{flow_class.__name__}' is a stub (_stub = True) and cannot be executed. "
            f"Implement the run() body and remove 'stub=True' from the class declaration to enable execution."
        )
    prefect_flow_fn = flow_class._prefect_flow_fn
    if _FlowRunContext.get() is not None and prefect_flow_fn is not None:
        raw_flow_result = cast(
            object,
            await prefect_flow_fn(
                flow_instance,
                **resolved_kwargs,
            ),
        )
    else:
        raw_flow_result = cast(
            object,
            await flow_instance.run(
                **resolved_kwargs,
            ),
        )
    if not isinstance(raw_flow_result, tuple):
        raise TypeError(
            f"PipelineFlow '{flow_class.__name__}' returned {type(raw_flow_result).__name__}. "
            "run() must return tuple[Document, ...]. "
            "Hint: for single-document returns use (doc,) with trailing comma, "
            "or wrap a list: return tuple(results)"
        )
    raw_result_docs = cast(tuple[object, ...], raw_flow_result)
    if not raw_result_docs:
        raise TypeError(
            f"PipelineFlow '{flow_class.__name__}' returned an empty tuple. run() must return at least one Document. Every flow must produce output documents."
        )
    if any(not isinstance(document, Document) for document in raw_result_docs):
        raise TypeError(f"PipelineFlow '{flow_class.__name__}' returned non-Document items. run() must return tuple[Document, ...].")
    validated_result = cast(tuple[Document, ...], raw_flow_result)
    input_documents = _documents_from_flow_arguments(resolved_kwargs)
    if input_documents and validated_result:
        input_sha256s = frozenset(document.sha256 for document in input_documents)
        passthrough_documents = [document for document in validated_result if document.sha256 in input_sha256s]
        if passthrough_documents:
            passthrough_names = ", ".join(f"'{document.name}'" for document in passthrough_documents)
            logger.warning(
                "PipelineFlow '%s' returned input document(s) unchanged: %s. "
                "Flows are the great filter of phase state: return only the handoff artifacts this phase produced. "
                "If a phase intentionally returns prior state as the honest phase result, keep it; otherwise remove the passthrough.",
                flow_class.__name__,
                passthrough_names,
            )
    return validated_result


def _matching_documents(docs: list[Document], annotation: Any) -> list[Document]:
    """Return blackboard documents matching the Document types referenced by ``annotation``."""
    document_types = collect_document_types(annotation)
    return [document for document in docs if any(isinstance(document, doc_type) for doc_type in document_types)]


def _resolve_flow_arguments(
    flow_class: type[PipelineFlow],
    docs: list[Document],
    options: FlowOptions,
    *,
    allow_partial: bool = False,
) -> dict[str, Any]:
    """Resolve flow ``run()`` kwargs from the accumulated blackboard."""
    hints = resolve_type_hints(flow_class.run)
    parameters = list(inspect.signature(flow_class.run).parameters.values())
    resolved: dict[str, Any] = {}

    for parameter in parameters[1:]:
        annotation = hints.get(parameter.name)
        if annotation is None:
            continue

        unwrapped = unwrap_optional(annotation)
        if isinstance(unwrapped, type) and issubclass(unwrapped, FlowOptions):
            resolved[parameter.name] = options
            continue

        matching = _matching_documents(docs, annotation)
        if get_origin(_unwrap_annotated(annotation)) is tuple:
            resolved[parameter.name] = tuple(matching)
            continue

        if matching:
            resolved[parameter.name] = matching[-1]
            continue

        if is_optional_annotation(annotation):
            resolved[parameter.name] = None
            continue

        if allow_partial:
            continue

        available = ", ".join(sorted({type(document).__name__ for document in docs})) or "(none)"
        raise TypeError(
            f"PipelineFlow '{flow_class.__name__}'.run parameter '{parameter.name}' "
            f"requires {annotation!r} but no matching document was found in the blackboard. "
            f"Available document types: {available}."
        )

    return resolved


def _documents_from_flow_arguments(arguments: dict[str, Any]) -> tuple[Document, ...]:
    """Collect the document inputs referenced by resolved flow kwargs."""
    collected: list[Document] = []
    for value in arguments.values():
        if isinstance(value, Document):
            collected.append(value)
            continue
        if isinstance(value, tuple):
            collected.extend(item for item in value if isinstance(item, Document))
    return _deduplicate_documents_by_sha256(collected)


async def _run_flow_with_retries(
    *,
    flow_instance: PipelineFlow,
    flow_class: type[PipelineFlow],
    flow_name: str,
    resolved_kwargs: dict[str, Any],
    current_exec_ctx: ExecutionContext | None,
    active_handles_before: set[object],
    step: int,
    total_steps: int,
    deployment_flow_retries: int | None = None,
    deployment_flow_retry_delay_seconds: int | None = None,
    parent_span_ctx: SpanContext,
) -> tuple[Document, ...]:
    """Execute a flow's run() with retries, exponential backoff, and NonRetriableError handling.

    Every executed body — including retries=0 — is wrapped in an ATTEMPT span so the span
    tree is FLOW → ATTEMPT → children. Retry count resolution: flow class override >
    deployment ``flow_retries`` > ``Settings.flow_retries``.
    """
    retries = _resolve_flow_retries(flow_class, deployment_flow_retries)
    retry_delay = _resolve_flow_retry_delay(flow_class, deployment_flow_retry_delay_seconds)
    flow_attempts = retries + 1

    for flow_attempt in range(flow_attempts):
        attempt_span_id = uuid7()
        try:
            async with track_span(
                SpanKind.ATTEMPT,
                f"attempt-{flow_attempt}",
                "",
                sinks=get_sinks(),
                span_id=attempt_span_id,
            ) as attempt_ctx:
                attempt_ctx.set_meta(attempt=flow_attempt, max_attempts=flow_attempts)
                return await _execute_single_flow_attempt(flow_class, flow_instance, resolved_kwargs)
        except NonRetriableError, TypeError, asyncio.CancelledError:
            raise
        except Exception as attempt_exc:
            if current_exec_ctx is not None:
                await _cancel_dispatched_handles(current_exec_ctx.active_task_handles, baseline_handles=active_handles_before)
            will_retry = flow_attempt < flow_attempts - 1
            delay = min(retry_delay * (2**flow_attempt), MAX_RETRY_DELAY_SECONDS) if will_retry else 0
            parent_span_ctx.record_retry_failure(
                exc=attempt_exc,
                attempt=flow_attempt,
                max_attempts=flow_attempts,
                attempt_span_id=str(attempt_span_id),
                will_retry=will_retry,
                delay_seconds=delay,
            )
            if not will_retry:
                raise
            logger.warning(
                "Flow %s attempt %d/%d failed, retrying in %ds",
                flow_name,
                flow_attempt + 1,
                flow_attempts,
                delay,
                exc_info=attempt_exc,
            )
            record_lifecycle_event(
                "flow.retry",
                f"Flow {flow_name} attempt {flow_attempt + 1}/{flow_attempts} failed, retrying in {delay}s",
                flow_name=flow_name,
                flow_class=flow_class.__name__,
                step=step,
                total_steps=total_steps,
                attempt=flow_attempt + 1,
                max_attempts=flow_attempts,
                delay_seconds=delay,
                error_type=type(attempt_exc).__name__,
                error_message=str(attempt_exc),
            )
            await asyncio.sleep(delay)

    raise RuntimeError(f"Flow {flow_name} completed without producing results")  # unreachable safety net


async def _execute_flow_with_context(
    *,
    flow_instance: PipelineFlow,
    flow_class: type[PipelineFlow],
    flow_name: str,
    current_docs: list[Document],
    options: FlowOptions,
    flow_exec_ctx: ExecutionContext | None,
    current_exec_ctx: ExecutionContext | None,
    active_handles_before: set[object],
    database: Any,
    publisher: ResultPublisher,
    deployment_span_id: UUID,
    run_id: str,
    flow_span_id: UUID,
    flow_cache_key: str,
    flow_options_payload: dict[str, Any],
    expected_tasks: list[dict[str, Any]],
    step: int,
    total_steps: int,
    root_id_str: str,
    parent_task_id_str: str | None,
    deployment_flow_retries: int | None = None,
    deployment_flow_retry_delay_seconds: int | None = None,
) -> tuple[Document, ...]:
    """Execute one flow under flow/task context and record failure state on exceptions."""
    with contextlib.ExitStack() as flow_scope:
        if flow_exec_ctx is not None:
            flow_scope.enter_context(set_execution_context(flow_exec_ctx))
        flow_scope.enter_context(set_task_context(TaskContext(scope_kind="flow", task_class_name=flow_class.__name__)))
        resolved_kwargs = _resolve_flow_arguments(flow_class, current_docs, options)
        input_documents = _documents_from_flow_arguments(resolved_kwargs)
        flow_target = f"instance_method:{flow_class.__module__}:{flow_class.__qualname__}.run"
        flow_receiver = {"mode": "constructor_args", "value": flow_instance.get_params()}
        flow_input = dict(resolved_kwargs)
        flow_input_preview = {
            "flow_class": flow_class.__name__,
            "flow_options": flow_options_payload,
            "input_documents": [document.name for document in input_documents],
            "input_parameters": tuple(resolved_kwargs),
        }
        flow_started_at = time.monotonic()

        input_doc_sha256s = tuple(doc.sha256 for doc in input_documents)
        deployment_span_id_str = str(deployment_span_id)

        validated_docs: tuple[Document, ...] = ()
        try:
            async with track_span(
                SpanKind.FLOW,
                flow_name,
                flow_target,
                sinks=get_sinks(),
                span_id=flow_span_id,
                parent_span_id=deployment_span_id,
                encode_receiver=flow_receiver,
                encode_input=flow_input,
                db=database,
                input_preview=flow_input_preview,
            ) as span_ctx:
                if flow_class.BASE_COST_USD > 0:
                    span_ctx._add_cost(flow_class.BASE_COST_USD)
                span_ctx.set_meta(
                    step=step,
                    total_steps=total_steps,
                    estimated_minutes=flow_instance.estimated_minutes,
                    expected_tasks=expected_tasks,
                    cache_hit=False,
                    cache_key=flow_cache_key,
                    flow_retries=_resolve_flow_retries(flow_class, deployment_flow_retries),
                    flow_retry_delay_seconds=_resolve_flow_retry_delay(flow_class, deployment_flow_retry_delay_seconds),
                )
                await publisher.publish_flow_started(
                    FlowStartedEvent(
                        run_id=run_id,
                        span_id=str(flow_span_id),
                        root_deployment_id=root_id_str,
                        parent_deployment_task_id=parent_task_id_str,
                        flow_name=flow_name,
                        flow_class=flow_class.__name__,
                        step=step,
                        total_steps=total_steps,
                        status=str(SpanStatus.RUNNING),
                        expected_tasks=expected_tasks,
                        flow_params=flow_instance.get_params(),
                        parent_span_id=deployment_span_id_str,
                        input_document_sha256s=input_doc_sha256s,
                    )
                )
                validated_docs = await _run_flow_with_retries(
                    flow_instance=flow_instance,
                    flow_class=flow_class,
                    flow_name=flow_name,
                    resolved_kwargs=resolved_kwargs,
                    current_exec_ctx=current_exec_ctx,
                    active_handles_before=active_handles_before,
                    step=step,
                    total_steps=total_steps,
                    deployment_flow_retries=deployment_flow_retries,
                    deployment_flow_retry_delay_seconds=deployment_flow_retry_delay_seconds,
                    parent_span_ctx=span_ctx,
                )
                span_ctx.set_output_preview({"documents": [document.name for document in validated_docs]})
                span_ctx._set_output_value(validated_docs)
        except (Exception, asyncio.CancelledError) as flow_exc:
            if current_exec_ctx is not None:
                await _cancel_dispatched_handles(current_exec_ctx.active_task_handles, baseline_handles=active_handles_before)
            flow_error_message = str(flow_exc)
            try:
                await publisher.publish_flow_failed(
                    FlowFailedEvent(
                        run_id=run_id,
                        span_id=str(flow_span_id),
                        root_deployment_id=root_id_str,
                        parent_deployment_task_id=parent_task_id_str,
                        flow_name=flow_name,
                        flow_class=flow_class.__name__,
                        step=step,
                        total_steps=total_steps,
                        status=str(SpanStatus.FAILED),
                        error_message=flow_error_message,
                        parent_span_id=deployment_span_id_str,
                        input_document_sha256s=input_doc_sha256s,
                    )
                )
            except _PUBLISH_EXCEPTIONS as publish_error:
                logger.warning("Failed to publish flow.failed event: %s", publish_error)
            raise

        flow_duration_ms = int((time.monotonic() - flow_started_at) * 1000)
        output_refs = tuple(
            DocumentRef(
                sha256=doc.sha256,
                class_name=type(doc).__name__,
                name=doc.name,
                summary=doc.summary,
                publicly_visible=getattr(type(doc), "publicly_visible", False),
                derived_from=tuple(doc.derived_from),
                triggered_by=tuple(doc.triggered_by),
            )
            for doc in validated_docs
        )
        await publisher.publish_flow_completed(
            FlowCompletedEvent(
                run_id=run_id,
                span_id=str(flow_span_id),
                root_deployment_id=root_id_str,
                parent_deployment_task_id=parent_task_id_str,
                flow_name=flow_name,
                flow_class=flow_class.__name__,
                step=step,
                total_steps=total_steps,
                status=str(SpanStatus.COMPLETED),
                duration_ms=flow_duration_ms,
                output_documents=output_refs,
                parent_span_id=deployment_span_id_str,
                input_document_sha256s=input_doc_sha256s,
            )
        )
        return validated_docs


def _safe_uuid(value: str) -> UUID | None:
    """Parse a UUID string, returning None if invalid."""
    try:
        return UUID(value)
    except ValueError, AttributeError:
        return None


def _deduplicate_documents_by_sha256(documents: Sequence[Document]) -> tuple[Document, ...]:
    """Deduplicate documents by SHA256 while preserving first-seen order."""
    deduped: dict[str, Document] = {}
    for document in documents:
        deduped.setdefault(document.sha256, document)
    return tuple(deduped.values())


def _required_flow_input_types(flow_cls: type[PipelineFlow]) -> list[type[Document]]:
    """Return required singleton document input types for a flow."""
    hints = resolve_type_hints(flow_cls.run)
    parameters = list(inspect.signature(flow_cls.run).parameters.values())
    required: list[type[Document]] = []
    for parameter in parameters[1:]:
        annotation = hints.get(parameter.name)
        if annotation is None:
            continue
        unwrapped = unwrap_optional(annotation)
        if isinstance(unwrapped, type) and issubclass(unwrapped, FlowOptions):
            continue
        if is_optional_annotation(annotation):
            continue
        if get_origin(_unwrap_annotated(annotation)) is tuple:
            continue
        required.extend(collect_document_types(annotation))
    return required


def _validate_flow_chain(deployment_name: str, flows: Sequence[PipelineFlow]) -> None:
    """Validate that each flow's input types are satisfiable by preceding flows' outputs."""
    type_pool: set[type[Document]] = set()

    for index, flow_instance in enumerate(flows):
        flow_cls = type(flow_instance)
        required_input_types = _required_flow_input_types(flow_cls)
        output_types = flow_cls.output_document_types
        flow_name = flow_instance.name

        if index == 0:
            type_pool.update(required_input_types)
        elif required_input_types:
            missing_required_types = [required for required in required_input_types if not any(issubclass(available, required) for available in type_pool)]
            if missing_required_types:
                input_names = sorted(document_type.__name__ for document_type in missing_required_types)
                pool_names = sorted(document_type.__name__ for document_type in type_pool) if type_pool else ["(empty)"]
                raise TypeError(
                    f"{deployment_name}: flow '{flow_name}' (step {index + 1}) requires input types "
                    f"{input_names} but they are not all produced by preceding flows. "
                    f"Available types: {pool_names}"
                )

        type_pool.update(output_types)


def _first_declaring_class(cls: type, attribute_name: str) -> type | None:
    """Return the first class in the MRO that declares ``attribute_name``."""
    for base in cls.__mro__:
        if attribute_name in base.__dict__:
            return base
    return None
