"""Remote deployment utilities for calling PipelineDeployment flows via Prefect."""

import asyncio
import importlib
import logging
import time
from collections.abc import Sequence
from typing import Any, ClassVar, Generic, TypeVar, cast, final
from uuid import UUID, uuid7

from prefect import get_client
from prefect.client.orchestration import PrefectClient
from prefect.client.schemas import FlowRun
from prefect.context import AsyncClientContext
from prefect.deployments.flow_runs import run_deployment
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
]

TOptions = TypeVar("TOptions", bound=FlowOptions, default=FlowOptions)
TResult = TypeVar("TResult", bound=DeploymentResult, default=DeploymentResult)

_POLL_INTERVAL = 5.0
_MAX_POLL_SECONDS = 7_200.0
_MAX_CONSECUTIVE_POLL_ERRORS = 10
_REMOTE_RUN_ID_FINGERPRINT_LENGTH = 8


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
            if publisher is not None and flow_frame is not None:
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
                            task_class=type(self).__name__,
                            parent_span_id=str(exec_ctx.flow_span_id) if exec_ctx is not None and exec_ctx.flow_span_id else "",
                            input_document_sha256s=input_sha256s,
                        )
                    )
                except (OSError, RuntimeError, ValueError, TypeError) as exc:
                    logger.warning("Remote task started event publish failed for '%s': %s", task_name, exc)
            try:
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
            except (Exception, asyncio.CancelledError) as exc:
                if publisher is not None and flow_frame is not None:
                    try:
                        await publisher.publish_task_failed(
                            TaskFailedEvent(
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
                                error_message=str(exc),
                                parent_span_id=str(exec_ctx.flow_span_id) if exec_ctx is not None and exec_ctx.flow_span_id else "",
                                input_document_sha256s=input_sha256s,
                            )
                        )
                    except (OSError, RuntimeError, ValueError, TypeError) as pub_exc:
                        logger.warning("Remote task failed event publish failed for '%s': %s", task_name, pub_exc)
                raise
            if publisher is not None and flow_frame is not None:
                try:
                    await publisher.publish_task_completed(
                        TaskCompletedEvent(
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
                        )
                    )
                except (OSError, RuntimeError, ValueError, TypeError) as pub_exc:
                    logger.warning("Remote task completed event publish failed for '%s': %s", task_name, pub_exc)
            span_ctx.set_output_preview(result.model_dump(mode="json"))
            span_ctx._set_output_value(result)
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
        )

        if isinstance(result, DeploymentResult):
            return cast(TResult, result)
        if isinstance(result, dict):
            return cast(TResult, self.result_type.model_validate(result))
        raise TypeError(f"Remote deployment '{self.name}' returned unexpected type: {type(result).__name__}")


async def _run_remote_deployment(
    deployment_name: str,
    parameters: dict[str, Any],
) -> Any:
    """Run a remote Prefect deployment and poll until completion."""
    async with get_client() as client:
        try:
            await client.read_deployment_by_name(name=deployment_name)
            fr: FlowRun = await run_deployment(  # type: ignore[assignment]
                client=client,
                name=deployment_name,
                parameters=parameters,
                as_subflow=True,
                timeout=0,
            )
            return await _poll_remote_flow_run(client, cast(UUID, fr.id))
        except ObjectNotFound:
            pass

    if not settings.prefect_api_url:
        raise ValueError(f"{deployment_name} not found, PREFECT_API_URL not set")

    async with PrefectClient(
        api=settings.prefect_api_url,
        api_key=settings.prefect_api_key,
        auth_string=settings.prefect_api_auth_string,
    ) as client:
        try:
            await client.read_deployment_by_name(name=deployment_name)
            ctx = AsyncClientContext.model_construct(client=client, _httpx_settings=None, _context_stack=0)
            with ctx:
                fr = await run_deployment(  # type: ignore[assignment]
                    client=client,
                    name=deployment_name,
                    parameters=parameters,
                    as_subflow=False,
                    timeout=0,
                )
                return await _poll_remote_flow_run(client, cast(UUID, fr.id))
        except ObjectNotFound:
            pass

    raise ValueError(f"{deployment_name} deployment not found")


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
    while True:
        if time.monotonic() - started_at >= max_poll_seconds:
            raise TimeoutError(
                f"Polling remote flow run {flow_run_id} exceeded {max_poll_seconds:.1f}s. "
                "Increase max_poll_seconds only if the remote deployment is expected to run longer."
            )
        try:
            flow_run = await client.read_flow_run(flow_run_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            consecutive_errors += 1
            if consecutive_errors >= _MAX_CONSECUTIVE_POLL_ERRORS:
                raise RuntimeError(
                    f"Polling remote flow run {flow_run_id} failed {_MAX_CONSECUTIVE_POLL_ERRORS} times in a row. "
                    "Check Prefect availability, credentials, and remote worker health."
                ) from None
            logger.warning("Failed to poll remote flow run %s", flow_run_id, exc_info=True)
            await asyncio.sleep(poll_interval)
            continue

        consecutive_errors = 0
        state = flow_run.state
        if state is not None and state.is_final():
            return await state.result()

        await asyncio.sleep(poll_interval)
