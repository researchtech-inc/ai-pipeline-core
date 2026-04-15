"""Remote deployment utilities for calling PipelineDeployment flows via Prefect."""

import asyncio
import importlib
import logging
import random
import time
from collections.abc import Sequence
from typing import Any, ClassVar, Generic, TypeVar, cast, final
from uuid import UUID, uuid7

from httpx import HTTPStatusError, RequestError
from prefect import get_client
from prefect.client.orchestration import PrefectClient
from prefect.client.schemas import FlowRun
from prefect.context import AsyncClientContext
from prefect.deployments.flow_runs import arun_deployment
from prefect.exceptions import ObjectNotFound

from ai_pipeline_core._lifecycle_events import TaskCompletedEvent, TaskFailedEvent, TaskStartedEvent
from ai_pipeline_core.database import SpanKind, SpanStatus
from ai_pipeline_core.deployment._helpers import (
    _compute_input_fingerprint,
    class_name_to_deployment_name,
    extract_generic_params,
    validate_run_id,
)
from ai_pipeline_core.documents import Document
from ai_pipeline_core.pipeline._execution_context import get_execution_context, get_run_id, get_sinks
from ai_pipeline_core.pipeline._track_span import track_span
from ai_pipeline_core.pipeline.options import FlowOptions
from ai_pipeline_core.settings import settings

from ._resolve import _DocumentInput
from .base import DeploymentResult

logger = logging.getLogger(__name__)

__all__ = [
    "RemoteDeployment",
    "RemoteDeploymentError",
    "RemoteDeploymentExecutionError",
    "RemoteDeploymentNotFoundError",
    "RemoteDeploymentPollingError",
    "RemoteDeploymentSubmissionError",
]

TOptions = TypeVar("TOptions", bound=FlowOptions, default=FlowOptions)
TResult = TypeVar("TResult", bound=DeploymentResult, default=DeploymentResult)

_POLL_INTERVAL = 5.0
_MAX_POLL_SECONDS = 10_800
_MAX_CONSECUTIVE_POLL_ERRORS = 10
_MAX_SUBMISSION_ATTEMPTS = 3
_INITIAL_SUBMISSION_RETRY_DELAY_SECONDS = 1.0
_MAX_SUBMISSION_RETRY_DELAY_SECONDS = 4.0
_SUBMISSION_RETRY_JITTER_SECONDS = 0.25
_MAX_RESPONSE_DETAIL_LENGTH = 500
_REMOTE_RUN_ID_FINGERPRINT_LENGTH = 8
_RETRYABLE_SUBMISSION_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class RemoteDeploymentError(RuntimeError):
    """Base error for remote deployment submission, polling, and execution failures."""


class RemoteDeploymentNotFoundError(RemoteDeploymentError):
    """Raised when a remote deployment cannot be found on any configured Prefect server."""


class RemoteDeploymentSubmissionError(RemoteDeploymentError):
    """Raised when remote flow-run creation fails before a flow run can be polled."""


class RemoteDeploymentPollingError(RemoteDeploymentError):
    """Raised when polling a created remote flow run fails repeatedly."""


class RemoteDeploymentExecutionError(RemoteDeploymentError):
    """Raised when a remote flow run reaches a non-successful terminal state."""


def _import_module_by_name(module_path: str) -> Any:
    return importlib.import_module(module_path)


def _derive_remote_run_id(run_id: str, documents: Sequence[Document], options: FlowOptions) -> str:
    """Deterministic run_id from caller's run_id + input fingerprint.

    Same documents + options produce the same derived run_id (enables worker resume).
    Different inputs produce different derived run_id (prevents collisions).
    """
    fingerprint = _compute_input_fingerprint(documents, options)[:_REMOTE_RUN_ID_FINGERPRINT_LENGTH]
    return f"{run_id}-{fingerprint}"


def _serialize_document_inputs(documents: Sequence[Document]) -> list[dict[str, Any]]:
    """Normalize documents to the _DocumentInput schema used by the Prefect flow."""
    return [_DocumentInput.model_validate(doc.serialize_model()).model_dump(mode="json") for doc in documents]


def _iterate_exception_chain(error: BaseException) -> tuple[BaseException, ...]:
    """Return the exception plus chained causes/contexts without revisiting nodes."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        chain.append(current)
        seen.add(id(current))
        current = current.__cause__ if current.__cause__ is not None else current.__context__
    return tuple(chain)


def _find_http_status_error(error: BaseException) -> HTTPStatusError | None:
    """Find an HTTP status error in the exception chain, if one exists."""
    for chained_error in _iterate_exception_chain(error):
        if isinstance(chained_error, HTTPStatusError):
            return chained_error
    return None


def _find_request_error(error: BaseException) -> RequestError | None:
    """Find a request transport error in the exception chain, if one exists."""
    for chained_error in _iterate_exception_chain(error):
        if isinstance(chained_error, RequestError):
            return chained_error
    return None


def _extract_response_detail(error: BaseException) -> str:
    """Extract an actionable HTTP response detail from a chained exception."""
    http_status_error = _find_http_status_error(error)
    if http_status_error is None:
        return str(error)

    try:
        detail = http_status_error.response.text.strip()
    except Exception:
        # Safe fallback: response-body decoding is diagnostic only. If reading the body fails,
        # we still preserve the original HTTP error via exception chaining and fall back to the
        # status-line text instead of masking the real submission or polling failure.
        detail = ""
    if detail:
        if len(detail) <= _MAX_RESPONSE_DETAIL_LENGTH:
            return detail
        return f"{detail[:_MAX_RESPONSE_DETAIL_LENGTH]}..."
    return str(http_status_error)


def _classify_remote_request_error(error: BaseException) -> tuple[bool, int | None]:
    """Return whether a remote HTTP or transport error is retryable and its HTTP status if available."""
    http_status_error = _find_http_status_error(error)
    if http_status_error is not None:
        status_code = http_status_error.response.status_code
        return status_code in _RETRYABLE_SUBMISSION_STATUS_CODES, status_code
    if _find_request_error(error) is not None:
        return True, None
    if any(isinstance(chained_error, TimeoutError) for chained_error in _iterate_exception_chain(error)):
        return True, None
    return False, None


def _submission_retry_delay_seconds(attempt_number: int) -> float:
    """Return exponential backoff delay for a 1-based submission attempt number."""
    exponential_factor = attempt_number - 1
    base_delay_seconds = _INITIAL_SUBMISSION_RETRY_DELAY_SECONDS * (2**exponential_factor)
    delay_seconds = base_delay_seconds + random.SystemRandom().uniform(0.0, _SUBMISSION_RETRY_JITTER_SECONDS)
    return min(delay_seconds, _MAX_SUBMISSION_RETRY_DELAY_SECONDS)


async def _submit_remote_flow_run(
    *,
    client: PrefectClient,
    deployment_name: str,
    parameters: dict[str, Any],
    idempotency_key: str,
    as_subflow: bool,
) -> FlowRun:
    """Submit a remote flow run with idempotent retry on transient Prefect failures."""
    last_error: Exception | None = None

    for attempt_number in range(1, _MAX_SUBMISSION_ATTEMPTS + 1):
        try:
            return await arun_deployment(
                client=client,
                name=deployment_name,
                parameters=parameters,
                as_subflow=as_subflow,
                timeout=0,
                idempotency_key=idempotency_key,
            )
        except asyncio.CancelledError:
            raise
        except ObjectNotFound:
            raise
        except Exception as exc:
            last_error = exc
            is_retryable, status_code = _classify_remote_request_error(exc)
            if not is_retryable:
                detail = _extract_response_detail(exc)
                status_text = f"HTTP {status_code}" if status_code is not None else type(exc).__name__
                raise RemoteDeploymentSubmissionError(
                    f"Remote deployment '{deployment_name}' submission failed with {status_text}. "
                    f"Check deployment parameters and Prefect configuration. Detail: {detail}"
                ) from exc

            if attempt_number == _MAX_SUBMISSION_ATTEMPTS:
                detail = _extract_response_detail(exc)
                status_text = f"HTTP {status_code}" if status_code is not None else type(exc).__name__
                raise RemoteDeploymentSubmissionError(
                    f"Remote deployment '{deployment_name}' submission failed after "
                    f"{_MAX_SUBMISSION_ATTEMPTS} attempts using idempotency key '{idempotency_key}'. "
                    f"Prefect reported {status_text}. Check Prefect availability and server logs. "
                    f"Last detail: {detail}"
                ) from exc

            delay_seconds = _submission_retry_delay_seconds(attempt_number)
            logger.warning(
                "Remote deployment '%s' submission attempt %d/%d failed%s. Retrying in %.1fs with idempotency key '%s'.",
                deployment_name,
                attempt_number,
                _MAX_SUBMISSION_ATTEMPTS,
                f" (HTTP {status_code})" if status_code is not None else "",
                delay_seconds,
                idempotency_key,
            )
            await asyncio.sleep(delay_seconds)

    raise RemoteDeploymentSubmissionError(f"Remote deployment '{deployment_name}' submission exhausted all retry attempts unexpectedly.") from last_error


async def _submit_and_poll_remote_flow_run(
    *,
    client: PrefectClient,
    deployment_name: str,
    parameters: dict[str, Any],
    idempotency_key: str,
    as_subflow: bool,
    max_poll_seconds: float = _MAX_POLL_SECONDS,
) -> Any:
    """Submit a remote flow run via Prefect and poll it to completion."""
    flow_run = await _submit_remote_flow_run(
        client=client,
        deployment_name=deployment_name,
        parameters=parameters,
        idempotency_key=idempotency_key,
        as_subflow=as_subflow,
    )
    return await _poll_remote_flow_run(client, flow_run.id, max_poll_seconds=max_poll_seconds)


async def _publish_remote_task_started(
    *,
    publisher: Any,
    flow_frame: Any,
    run_id: str,
    subtask_span_id: UUID,
    resolved_root_id: UUID,
    parent_deployment_task_id: UUID | None,
    flow_name: str,
    flow_step: int,
    total_steps: int,
    task_name: str,
    task_class_name: str,
    parent_span_id: str,
    input_sha256s: tuple[str, ...],
) -> None:
    if publisher is None or flow_frame is None:
        return
    try:
        await publisher.publish_task_started(
            TaskStartedEvent(
                run_id=run_id,
                span_id=str(subtask_span_id),
                root_deployment_id=str(resolved_root_id),
                parent_deployment_task_id=str(parent_deployment_task_id) if parent_deployment_task_id else None,
                flow_name=flow_name,
                step=flow_step,
                total_steps=total_steps,
                status=str(SpanStatus.RUNNING),
                task_name=task_name,
                task_class=task_class_name,
                parent_span_id=parent_span_id,
                input_document_sha256s=input_sha256s,
            )
        )
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        logger.warning("Remote task started event publish failed for '%s': %s", task_name, exc)


async def _publish_remote_task_terminal(
    *,
    publisher: Any,
    flow_frame: Any,
    event: TaskCompletedEvent | TaskFailedEvent,
    terminal_kind: str,
) -> None:
    if publisher is None or flow_frame is None:
        return
    publish_method = publisher.publish_task_completed if isinstance(event, TaskCompletedEvent) else publisher.publish_task_failed
    try:
        await publish_method(event)
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        logger.warning("Remote task %s event publish failed for '%s': %s", terminal_kind, event.task_name, exc)


class RemoteDeployment(Generic[TOptions, TResult]):
    """Typed client for calling a remote PipelineDeployment via Prefect.

    Generic parameters:
        TOptions: FlowOptions subclass for the deployment.
        TResult: DeploymentResult subclass returned by the deployment.

    Set ``deployment_class`` to enable inline mode (test/local):
        deployment_class = "module.path:ClassName"
    """

    name: ClassVar[str]
    options_type: ClassVar[type[FlowOptions]]
    result_type: ClassVar[type[DeploymentResult]]
    deployment_class: ClassVar[str] = ""
    max_poll_seconds: ClassVar[int] = _MAX_POLL_SECONDS

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)

        # Auto-derive name unless explicitly set in class body
        if "name" not in cls.__dict__:
            cls.name = class_name_to_deployment_name(cls.__name__)

        # Extract Generic params: (TOptions, TResult)
        generic_args = extract_generic_params(cls, RemoteDeployment)
        if len(generic_args) < 2:
            raise TypeError(f"{cls.__name__} must specify 2 Generic parameters: class {cls.__name__}(RemoteDeployment[OptionsType, ResultType])")

        options_type, result_type = generic_args[0], generic_args[1]

        if not isinstance(options_type, type) or not issubclass(options_type, FlowOptions):
            raise TypeError(f"{cls.__name__}: first Generic param must be a FlowOptions subclass, got {options_type}")
        if not isinstance(result_type, type) or not issubclass(result_type, DeploymentResult):
            raise TypeError(f"{cls.__name__}: second Generic param must be a DeploymentResult subclass, got {result_type}")

        cls.options_type = options_type
        cls.result_type = result_type

        if cls.max_poll_seconds <= 0:
            raise TypeError(
                f"{cls.__name__}.max_poll_seconds must be > 0, got {cls.max_poll_seconds}. "
                "Set a positive value representing the maximum seconds to poll the remote deployment."
            )

    @property
    def deployment_path(self) -> str:
        """Full Prefect deployment path: '{flow_name}/{deployment_name}'."""
        return f"{self.name}/{self.name.replace('-', '_')}"

    def _resolve_deployment_class(self) -> Any:
        """Import the actual PipelineDeployment class for inline execution."""
        if not self.deployment_class:
            raise ValueError(
                f"{type(self).__name__}.deployment_class is not set. Set deployment_class = 'module.path:ClassName' to enable inline/test execution."
            )
        module_path, class_name = self.deployment_class.rsplit(":", 1)
        module = _import_module_by_name(module_path)
        return getattr(module, class_name)

    @final
    async def run(
        self,
        documents: tuple[Document, ...],
        options: TOptions,
    ) -> TResult:
        """Execute the remote deployment.

        Uses inline mode when the active database backend cannot support remote execution,
        and Prefect remote mode when it can.
        """
        run_id = get_run_id()
        validate_run_id(run_id)
        derived_run_id = _derive_remote_run_id(run_id, documents, options)
        validate_run_id(derived_run_id)

        # Get execution context for DAG linking
        exec_ctx = get_execution_context()
        database = exec_ctx.database if exec_ctx else None
        deployment_id = exec_ctx.deployment_id if exec_ctx else None
        root_deployment_id = exec_ctx.root_deployment_id if exec_ctx else None

        subtask_span_id = uuid7()
        parent_span_id = (exec_ctx.current_span_id or deployment_id) if exec_ctx is not None else deployment_id
        sequence_no = exec_ctx.next_child_sequence(parent_span_id) if exec_ctx is not None and parent_span_id is not None else 0
        deployment_name = exec_ctx.deployment_name if exec_ctx is not None else ""

        # Determine backend mode
        use_inline = database is None
        inline_reason = "no execution database context is active"
        if database is not None and not database.supports_remote:
            use_inline = True
            inline_reason = "the active database backend does not support remote execution"
        if self.deployment_class:
            use_inline = True
            inline_reason = "deployment_class is set (inline execution configured)"

        publisher = exec_ctx.publisher if exec_ctx else None
        flow_frame = exec_ctx.flow_frame if exec_ctx else None
        flow_step = flow_frame.step if flow_frame is not None else 0
        total_steps = flow_frame.total_steps if flow_frame is not None else 0
        flow_name = flow_frame.name if flow_frame is not None else ""
        parent_deployment_task_id = exec_ctx.parent_deployment_task_id if exec_ctx else None
        input_sha256s = tuple(doc.sha256 for doc in documents)
        task_name = f"remote:{self.name}"
        task_start = time.monotonic()
        resolved_root_id = root_deployment_id or deployment_id or subtask_span_id

        result: Any | None = None
        try:
            async with track_span(
                SpanKind.TASK,
                task_name,
                "",
                sinks=get_sinks(),
                span_id=subtask_span_id,
                parent_span_id=parent_span_id,
                encode_input={"documents": tuple(documents), "options": options},
                db=database,
                input_preview={"deployment": self.name, "document_count": len(documents)},
            ) as span_ctx:
                span_ctx.set_meta(
                    deployment_name=deployment_name,
                    remote_mode="inline" if use_inline else "prefect",
                    sequence_no=sequence_no,
                )
                await _publish_remote_task_started(
                    publisher=publisher,
                    flow_frame=flow_frame,
                    run_id=run_id,
                    subtask_span_id=subtask_span_id,
                    resolved_root_id=resolved_root_id,
                    parent_deployment_task_id=parent_deployment_task_id,
                    flow_name=flow_name,
                    flow_step=flow_step,
                    total_steps=total_steps,
                    task_name=task_name,
                    task_class_name=type(self).__name__,
                    parent_span_id=str(exec_ctx.flow_span_id) if exec_ctx is not None and exec_ctx.flow_span_id else "",
                    input_sha256s=input_sha256s,
                )
                if use_inline:
                    logger.warning(
                        "RemoteDeployment '%s' is falling back to inline execution because %s. "
                        "Configure a non-local deployment database to force Prefect remote execution.",
                        self.name,
                        inline_reason,
                    )
                    result = await self._run_inline(
                        derived_run_id,
                        documents,
                        options,
                        root_deployment_id=resolved_root_id,
                        parent_deployment_task_id=subtask_span_id,
                        database=database,
                        publisher=publisher,
                        parent_execution_id=exec_ctx.execution_id if exec_ctx else None,
                    )
                else:
                    result = await self._run_remote(
                        derived_run_id,
                        documents,
                        options,
                        root_deployment_id=resolved_root_id,
                        parent_deployment_task_id=subtask_span_id,
                        parent_execution_id=exec_ctx.execution_id if exec_ctx else None,
                    )
                span_ctx.set_output_preview(result.model_dump(mode="json"))
                span_ctx._set_output_value(result)
        except (Exception, asyncio.CancelledError) as task_failure:
            await _publish_remote_task_terminal(
                publisher=publisher,
                flow_frame=flow_frame,
                event=TaskFailedEvent(
                    run_id=run_id,
                    span_id=str(subtask_span_id),
                    root_deployment_id=str(resolved_root_id),
                    parent_deployment_task_id=str(parent_deployment_task_id) if parent_deployment_task_id else None,
                    flow_name=flow_name,
                    step=flow_step,
                    total_steps=total_steps,
                    status=str(SpanStatus.FAILED),
                    task_name=task_name,
                    task_class=type(self).__name__,
                    error_message=str(task_failure),
                    parent_span_id=str(exec_ctx.flow_span_id) if exec_ctx is not None and exec_ctx.flow_span_id else "",
                    input_document_sha256s=input_sha256s,
                ),
                terminal_kind="failed",
            )
            raise
        assert result is not None
        await _publish_remote_task_terminal(
            publisher=publisher,
            flow_frame=flow_frame,
            event=TaskCompletedEvent(
                run_id=run_id,
                span_id=str(subtask_span_id),
                root_deployment_id=str(resolved_root_id),
                parent_deployment_task_id=str(parent_deployment_task_id) if parent_deployment_task_id else None,
                flow_name=flow_name,
                step=flow_step,
                total_steps=total_steps,
                status=str(SpanStatus.COMPLETED),
                task_name=task_name,
                task_class=type(self).__name__,
                duration_ms=int((time.monotonic() - task_start) * 1000),
                parent_span_id=str(exec_ctx.flow_span_id) if exec_ctx is not None and exec_ctx.flow_span_id else "",
                input_document_sha256s=input_sha256s,
            ),
            terminal_kind="completed",
        )
        return result

    async def _run_inline(
        self,
        run_id: str,
        documents: Sequence[Document],
        options: TOptions,
        *,
        root_deployment_id: UUID,
        parent_deployment_task_id: UUID,
        database: Any,
        publisher: Any = None,
        parent_execution_id: UUID | None = None,
    ) -> TResult:
        """Run the deployment inline (same process) for test/local mode."""
        deployment_cls = self._resolve_deployment_class()
        deployment_instance = deployment_cls()

        result = await deployment_instance._run_with_context(
            run_id,
            documents,
            options,
            root_deployment_id=root_deployment_id,
            parent_deployment_task_id=parent_deployment_task_id,
            publisher=publisher,
            parent_execution_id=parent_execution_id,
            database=database,
        )

        if isinstance(result, DeploymentResult):
            return cast(TResult, result)
        if isinstance(result, dict):
            return cast(TResult, self.result_type.model_validate(result))
        raise TypeError(f"Inline deployment '{self.name}' returned unexpected type: {type(result).__name__}")

    async def _run_remote(
        self,
        run_id: str,
        documents: Sequence[Document],
        options: TOptions,
        *,
        root_deployment_id: UUID,
        parent_deployment_task_id: UUID,
        parent_execution_id: UUID | None = None,
    ) -> TResult:
        """Run the deployment remotely via Prefect."""
        parameters: dict[str, Any] = {
            "run_id": run_id,
            "document_inputs": _serialize_document_inputs(documents),
            "options": options,
            "parent_execution_id": str(parent_execution_id) if parent_execution_id is not None else None,
            "parent_deployment_task_id": str(parent_deployment_task_id),
            "root_deployment_id": str(root_deployment_id),
        }

        result = await _run_remote_deployment(
            self.deployment_path,
            parameters,
            max_poll_seconds=self.max_poll_seconds,
        )

        if isinstance(result, DeploymentResult):
            return cast(TResult, result)
        if isinstance(result, dict):
            return cast(TResult, self.result_type.model_validate(result))
        raise TypeError(f"Remote deployment '{self.name}' returned unexpected type: {type(result).__name__}")


async def _run_remote_deployment(
    deployment_name: str,
    parameters: dict[str, Any],
    *,
    max_poll_seconds: float = _MAX_POLL_SECONDS,
) -> Any:
    """Run a remote Prefect deployment and poll until completion."""
    run_id = parameters.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError(
            "Remote deployment parameters must include a non-empty 'run_id'. "
            "Pass the derived deployment run_id so Prefect retries can use it as the idempotency key."
        )

    async with get_client() as client:
        try:
            return await _submit_and_poll_remote_flow_run(
                client=client,
                deployment_name=deployment_name,
                parameters=parameters,
                idempotency_key=run_id,
                as_subflow=True,
                max_poll_seconds=max_poll_seconds,
            )
        except ObjectNotFound:
            pass

    if not settings.prefect_api_url:
        raise RemoteDeploymentNotFoundError(
            f"Remote deployment '{deployment_name}' was not found on the active Prefect client and "
            "`PREFECT_API_URL` is not configured. Deploy it first or set `PREFECT_API_URL` so the "
            "framework can reach the correct Prefect server."
        )

    async with PrefectClient(
        api=settings.prefect_api_url,
        api_key=settings.prefect_api_key,
        auth_string=settings.prefect_api_auth_string,
    ) as client:
        try:
            ctx = AsyncClientContext.model_construct(client=client, _httpx_settings=None, _context_stack=0)
            with ctx:
                return await _submit_and_poll_remote_flow_run(
                    client=client,
                    deployment_name=deployment_name,
                    parameters=parameters,
                    idempotency_key=run_id,
                    as_subflow=False,
                    max_poll_seconds=max_poll_seconds,
                )
        except ObjectNotFound as exc:
            raise RemoteDeploymentNotFoundError(
                f"Remote deployment '{deployment_name}' was not found on the active Prefect client or on "
                f"the configured Prefect API '{settings.prefect_api_url}'. Deploy it first or verify that "
                "`PREFECT_API_URL`, `PREFECT_API_KEY`, and `PREFECT_API_AUTH_STRING` point to the correct workspace."
            ) from exc


async def _poll_remote_flow_run(
    client: PrefectClient,
    flow_run_id: UUID,
    *,
    poll_interval: float = _POLL_INTERVAL,
    max_poll_seconds: float = _MAX_POLL_SECONDS,
) -> Any:
    """Poll a remote flow run until final state."""
    started_at = time.monotonic()
    consecutive_errors = 0
    first_error: Exception | None = None
    last_error: Exception | None = None
    while True:
        if time.monotonic() - started_at >= max_poll_seconds:
            raise RemoteDeploymentPollingError(
                f"Polling remote flow run {flow_run_id} exceeded {max_poll_seconds}s. "
                "Increase RemoteDeployment.max_poll_seconds on your deployment subclass, "
                "or inspect the Prefect UI to confirm the remote flow run is still progressing."
            )
        try:
            flow_run = await client.read_flow_run(flow_run_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            is_retryable, status_code = _classify_remote_request_error(exc)
            if not is_retryable:
                detail = _extract_response_detail(exc)
                status_text = f"HTTP {status_code}" if status_code is not None else type(exc).__name__
                raise RemoteDeploymentPollingError(
                    f"Polling remote flow run {flow_run_id} failed with non-retryable {status_text}. "
                    f"Check Prefect credentials, flow-run existence, and API configuration. Detail: {detail}"
                ) from exc

            consecutive_errors += 1
            if first_error is None:
                first_error = exc
            last_error = exc
            if consecutive_errors >= _MAX_CONSECUTIVE_POLL_ERRORS:
                assert first_error is not None
                assert last_error is not None
                raise RemoteDeploymentPollingError(
                    f"Polling remote flow run {flow_run_id} failed {_MAX_CONSECUTIVE_POLL_ERRORS} times in a row. "
                    f"First error: {type(first_error).__name__}: {first_error}. "
                    f"Last error: {type(last_error).__name__}: {last_error}. "
                    "Check Prefect availability, credentials, and remote worker health."
                ) from last_error
            logger.warning("Failed to poll remote flow run %s", flow_run_id, exc_info=True)
            await asyncio.sleep(poll_interval)
            continue

        consecutive_errors = 0
        first_error = None
        last_error = None
        state = flow_run.state
        if state is not None and state.is_final():
            if state.is_completed():
                try:
                    return await state.result()
                except Exception as exc:
                    raise RemoteDeploymentExecutionError(
                        f"Remote flow run {flow_run_id} reached a completed state but result retrieval failed. "
                        "Check the remote result store configuration and worker logs."
                    ) from exc

            try:
                await state.result()
            except Exception as exc:
                raise RemoteDeploymentExecutionError(
                    f"Remote flow run {flow_run_id} finished with terminal state '{state.name}' "
                    f"({state.type}). Check the remote worker logs for the underlying failure."
                ) from exc

            raise RemoteDeploymentExecutionError(
                f"Remote flow run {flow_run_id} finished with terminal state '{state.name}' ({state.type}). "
                "Check the Prefect UI and remote worker logs for details."
            )

        await asyncio.sleep(poll_interval)
