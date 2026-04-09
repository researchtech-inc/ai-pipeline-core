"""Helper functions for pipeline deployments."""

import asyncio
import contextlib
import hashlib
import json
import logging
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

from ai_pipeline_core.database import LogRecord
from ai_pipeline_core.database._factory import Database, create_database_from_settings
from ai_pipeline_core.documents import Document
from ai_pipeline_core.exceptions import LLMError, NonRetriableError, PipelineCoreError
from ai_pipeline_core.logger._buffer import MAX_PENDING_EXECUTION_LOGS, ExecutionLogBuffer
from ai_pipeline_core.logger._handler import SKIP_EXECUTION_LOG_ATTR, ExecutionLogHandler
from ai_pipeline_core.pipeline._log_sink import DatabaseLogSink
from ai_pipeline_core.pipeline._parallel import TaskHandle
from ai_pipeline_core.pipeline._types import LogSink
from ai_pipeline_core.pipeline.options import FlowOptions
from ai_pipeline_core.settings import Settings

from ._pubsub import PubSubPublisher
from ._types import ErrorCode, ResultPublisher, _NoopPublisher

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_RUN_ID_LENGTH",
    "SKIP_EXECUTION_LOG_ATTR",
    "_CLI_FIELDS",
    "_HANDLE_CANCEL_GRACE_SECONDS",
    "_HEARTBEAT_INTERVAL_SECONDS",
    "_MILLISECONDS_PER_SECOND",
    "_build_flow_cache_key",
    "_cancel_dispatched_handles",
    "_classify_error",
    "_compute_input_fingerprint",
    "_create_publisher",
    "_create_span_database_from_settings",
    "_ensure_execution_log_handler_installed",
    "_heartbeat_loop",
    "_log_flush_loop",
    "class_name_to_deployment_name",
    "extract_generic_params",
    "validate_run_id",
]

_RUN_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
MAX_RUN_ID_LENGTH = 100

# Fields added by run_cli()'s _CliOptions that should not affect fingerprints (run scope or remote run_id)
_CLI_FIELDS: frozenset[str] = frozenset({"working_directory", "run_id", "start", "end"})

_HEARTBEAT_INTERVAL_SECONDS = 30
_MILLISECONDS_PER_SECOND = 1000
_HANDLE_CANCEL_GRACE_SECONDS = 5
LOG_BUFFER_FLUSH_INTERVAL_SECONDS = 2
_execution_log_handler_lock = Lock()


def _trim_pending_logs(pending_logs: list[LogRecord]) -> tuple[list[LogRecord], int]:
    """Cap pending logs and report how many oldest entries were dropped."""
    if len(pending_logs) <= MAX_PENDING_EXECUTION_LOGS:
        return pending_logs, 0
    dropped_count = len(pending_logs) - MAX_PENDING_EXECUTION_LOGS
    return pending_logs[dropped_count:], dropped_count


def validate_run_id(run_id: str) -> None:
    """Validate run_id: alphanumeric + underscore + hyphen, 1-100 chars.

    Must be called at deployment entry points (PipelineDeployment.run, RemoteDeployment._execute, CLI).
    """
    if not run_id:
        raise ValueError("run_id must not be empty")
    if len(run_id) > MAX_RUN_ID_LENGTH:
        raise ValueError(
            f"run_id '{run_id[:20]}...' is {len(run_id)} chars, max is {MAX_RUN_ID_LENGTH}. Shorten the base run_id before passing to the deployment."
        )
    if not _RUN_ID_PATTERN.match(run_id):
        raise ValueError(
            f"run_id '{run_id}' contains invalid characters. "
            f"Only alphanumeric characters, underscores, and hyphens are allowed (pattern: {_RUN_ID_PATTERN.pattern})."
        )


def build_auto_run_id(*, output_dir_name: str, documents: Sequence[Document], options: FlowOptions) -> str:
    """Generate a deterministic run_id: {sanitized_dir}-{DDMMYY}-{hash8}.

    Same inputs on the same day in the same output directory produce the same run_id,
    enabling natural resume semantics. Different inputs or dates produce different IDs.
    """
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "-", output_dir_name).strip("-")[:40]
    if not sanitized:
        sanitized = "run"
    date_part = datetime.now(UTC).strftime("%d%m%y")
    hash_part = _compute_input_fingerprint(documents, options)[:8]
    return f"{sanitized}-{date_part}-{hash_part}"


def class_name_to_deployment_name(class_name: str) -> str:
    """Convert PascalCase to kebab-case: ResearchPipeline -> research-pipeline."""
    name = re.sub(r"(?<!^)(?=[A-Z])", "-", class_name)
    return name.lower()


def extract_generic_params(cls: type, base_class: type) -> tuple[Any, ...]:
    """Extract Generic type arguments from a class's base.

    Works with any number of Generic parameters (2 for PipelineDeployment, 3 for RemoteDeployment).
    Returns () if the base class is not found in __orig_bases__.
    """
    for base in getattr(cls, "__orig_bases__", []):
        origin = getattr(base, "__origin__", None)
        if origin is base_class:
            args = getattr(base, "__args__", ())
            if args:
                return args

    return ()


def _classify_error(exc: BaseException) -> ErrorCode:
    """Map exception to ErrorCode enum value.

    Unwraps NonRetriableError to classify the underlying cause so that
    e.g. NonRetriableError wrapping LLMError still produces PROVIDER_ERROR.
    """
    target = exc.__cause__ if isinstance(exc, NonRetriableError) and exc.__cause__ is not None else exc
    if isinstance(target, LLMError):
        return ErrorCode.PROVIDER_ERROR
    if isinstance(target, asyncio.CancelledError):
        return ErrorCode.CANCELLED
    if isinstance(target, TimeoutError):
        return ErrorCode.DURATION_EXCEEDED
    if isinstance(target, (ValueError, TypeError)):
        return ErrorCode.INVALID_INPUT
    if isinstance(target, PipelineCoreError):
        return ErrorCode.PIPELINE_ERROR
    return ErrorCode.UNKNOWN


def _create_publisher(settings_obj: Settings, service_type: str) -> ResultPublisher:
    """Create publisher based on environment and deployment configuration.

    Returns PubSubPublisher when Pub/Sub is configured and service_type is set,
    _NoopPublisher otherwise.
    """
    if not service_type:
        return _NoopPublisher()
    if settings_obj.pubsub_project_id and settings_obj.pubsub_topic_id:
        return PubSubPublisher(
            project_id=settings_obj.pubsub_project_id,
            topic_id=settings_obj.pubsub_topic_id,
            service_type=service_type,
        )
    return _NoopPublisher()


def _compute_input_fingerprint(documents: Sequence[Document], options: FlowOptions) -> str:
    """Compute the redesign input fingerprint from sorted input SHAs and serialized options."""
    exclude = set(_CLI_FIELDS & set(type(options).model_fields))
    options_json = options.model_dump_json(exclude=exclude, exclude_none=True)
    sha256s = sorted(doc.sha256 for doc in documents)
    fingerprint_source = "\n".join([*sha256s, options_json])
    return hashlib.sha256(fingerprint_source.encode()).hexdigest()[:16]


def _build_flow_cache_key(
    *,
    input_fingerprint: str,
    flow_class: type[Any],
    step: int,
    flow_params: dict[str, Any],
) -> str:
    """Build the deterministic cross-deployment flow cache key."""
    base = f"flow:{input_fingerprint}:{flow_class.__module__}:{flow_class.__qualname__}:{step}"
    if flow_params:
        params_json = json.dumps(flow_params, sort_keys=True, default=repr)
        params_hash = hashlib.sha256(params_json.encode()).hexdigest()[:12]
        return f"{base}:{params_hash}"
    return base


def _create_span_database_from_settings(
    settings_obj: Settings,
    *,
    base_path: Path | None = None,
) -> Database:
    """Create a span-era database backend from settings."""
    return create_database_from_settings(settings_obj, base_path=base_path)


def _ensure_execution_log_handler_installed() -> None:
    """Install the process-wide execution log handler on the root logger once."""
    root_logger = logging.getLogger()
    with _execution_log_handler_lock:
        if any(isinstance(handler, ExecutionLogHandler) for handler in root_logger.handlers):
            return
        root_logger.addHandler(ExecutionLogHandler())


async def _cancel_dispatched_handles(
    active_handles: set[object],
    *,
    baseline_handles: set[object],
) -> None:
    """Cancel handles dispatched within a flow and wait briefly for shutdown."""
    new_handles: list[TaskHandle[tuple[Document[Any], ...]]] = [
        handle for handle in list(active_handles) if handle not in baseline_handles and isinstance(handle, TaskHandle)
    ]
    if not new_handles:
        return

    for handle in new_handles:
        handle.cancel()

    pending_tasks: list[asyncio.Task[tuple[Document[Any], ...]]] = [handle._task for handle in new_handles if not handle.done]
    if pending_tasks:
        _done, pending = await asyncio.wait(pending_tasks, timeout=_HANDLE_CANCEL_GRACE_SECONDS)
        if pending:
            for task in pending:
                task.cancel()
            with contextlib.suppress(Exception):
                await asyncio.gather(*pending, return_exceptions=True)

    for handle in new_handles:
        active_handles.discard(handle)


async def _flush_logs_once(
    log_sinks: tuple[LogSink, ...],
    log_buffer: ExecutionLogBuffer | None,
    pending_logs: list[LogRecord],
) -> list[LogRecord]:
    """Drain buffered logs, save them, and retain any batch that failed to persist."""
    if log_buffer is not None:
        pending_logs.extend(log_buffer.drain())
    pending_logs, dropped_from_backlog = _trim_pending_logs(pending_logs)
    if dropped_from_backlog > 0:
        logger.warning(
            "Execution log backlog exceeded %d entries. Dropping %d oldest log(s) to keep memory bounded while database writes are failing.",
            MAX_PENDING_EXECUTION_LOGS,
            dropped_from_backlog,
            extra={SKIP_EXECUTION_LOG_ATTR: True},
        )
    if not pending_logs or not log_sinks:
        return pending_logs
    primary_sink = next((sink for sink in log_sinks if isinstance(sink, DatabaseLogSink)), None)
    if primary_sink is not None:
        try:
            await primary_sink.on_logs_batch(pending_logs)
        except Exception as exc:
            logger.warning(
                "Execution log flush failed. The framework will retry on the next flush cycle. Error: %s",
                exc,
                extra={SKIP_EXECUTION_LOG_ATTR: True},
                exc_info=exc,
            )
            return pending_logs
    for sink in log_sinks:
        if sink is primary_sink:
            continue
        try:
            await sink.on_logs_batch(pending_logs)
        except Exception as exc:
            logger.warning(
                "Best-effort log sink %s failed: %s",
                type(sink).__name__,
                exc,
                extra={SKIP_EXECUTION_LOG_ATTR: True},
                exc_info=exc,
            )
    if log_buffer is not None:
        dropped_from_buffer = log_buffer.consume_dropped_count()
        if dropped_from_buffer > 0:
            logger.warning(
                "Execution log buffer exceeded %d entries. Dropped %d oldest log(s) before persistence. "
                "Increase database reliability or flush logs more frequently.",
                MAX_PENDING_EXECUTION_LOGS,
                dropped_from_buffer,
                extra={SKIP_EXECUTION_LOG_ATTR: True},
            )
    return []


async def _log_flush_loop(
    log_sinks: tuple[LogSink, ...],
    log_buffer: ExecutionLogBuffer | None,
    flush_event: asyncio.Event,
) -> None:
    """Flush buffered execution logs on a timer or when the buffer reaches capacity."""
    pending_logs: list[LogRecord] = []
    try:
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(flush_event.wait(), timeout=LOG_BUFFER_FLUSH_INTERVAL_SECONDS)
            flush_event.clear()
            pending_logs = await _flush_logs_once(log_sinks, log_buffer, pending_logs)
    except asyncio.CancelledError:
        pending_logs = await _flush_logs_once(log_sinks, log_buffer, pending_logs)
        if pending_logs:
            logger.warning(
                "Execution log flush stopped with %d pending log(s). The database write path is still failing; inspect earlier log flush warnings.",
                len(pending_logs),
                extra={SKIP_EXECUTION_LOG_ATTR: True},
            )
        raise


async def _heartbeat_loop(publisher: ResultPublisher, run_id: str, *, root_deployment_id: str, span_id: str) -> None:
    """Publish heartbeat signals at regular intervals until cancelled."""
    while True:
        await asyncio.sleep(_HEARTBEAT_INTERVAL_SECONDS)
        try:
            await publisher.publish_heartbeat(run_id, root_deployment_id=root_deployment_id, span_id=span_id)
        except Exception as e:
            logger.warning("Heartbeat publish failed: %s", e)
        logger.info(
            "run.heartbeat",
            extra={
                "category": "lifecycle",
                "lifecycle": True,
                "event_type": "run.heartbeat",
                "run_id": run_id,
                "root_deployment_id": root_deployment_id,
                "span_id": span_id,
            },
        )
