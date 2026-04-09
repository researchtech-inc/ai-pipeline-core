"""Sentry span and log sinks."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import sentry_sdk
from ai_pipeline_core.database import SpanKind
from ai_pipeline_core.logger._types import LogRecord
from ai_pipeline_core.pipeline._execution_context import get_execution_context
from ai_pipeline_core.pipeline._span_types import SpanMetrics
from ai_pipeline_core.pipeline._types import LogSink

__all__ = ["SentryLogSink", "SentrySpanSink"]


@dataclass(frozen=True, slots=True)
class _StartedSpanState:
    kind: SpanKind
    name: str


class SentrySpanSink:
    """Capture terminal span failures in Sentry and add attempt breadcrumbs."""

    def __init__(self) -> None:
        self._started: dict[UUID, _StartedSpanState] = {}
        self._captured: set[UUID] = set()

    async def on_span_started(
        self,
        *,
        span_id: UUID,
        parent_span_id: UUID | None,
        kind: SpanKind,
        name: str,
        target: str,
        started_at: datetime,
        receiver_json: str,
        input_json: str,
        input_document_shas: frozenset[str],
        input_blob_shas: frozenset[str],
        input_preview: Any | None,
    ) -> None:
        _ = (
            parent_span_id,
            target,
            started_at,
            receiver_json,
            input_json,
            input_document_shas,
            input_blob_shas,
            input_preview,
        )
        self._started[span_id] = _StartedSpanState(kind=kind, name=name)

    async def on_span_finished(
        self,
        *,
        span_id: UUID,
        ended_at: datetime,
        output_json: str,
        error_json: str,
        output_document_shas: frozenset[str],
        output_blob_shas: frozenset[str],
        output_preview: Any | None,
        error: BaseException | None,
        metrics: SpanMetrics,
        meta: dict[str, Any],
    ) -> None:
        _ = ended_at, output_json, error_json, output_document_shas, output_blob_shas, output_preview, metrics
        started_state = self._started.pop(span_id, None)
        if error is None or started_state is None or span_id in self._captured:
            return
        self._captured.add(span_id)
        execution_ctx = get_execution_context()
        if started_state.kind == SpanKind.ATTEMPT:
            sentry_sdk.add_breadcrumb(
                category="attempt",
                level="warning",
                message=started_state.name,
                data={
                    "attempt": meta.get("attempt"),
                    "max_attempts": meta.get("max_attempts"),
                    "span_id": str(span_id),
                    "span_name": started_state.name,
                    "run_id": execution_ctx.run_id if execution_ctx is not None else "",
                    "error_type": type(error).__name__,
                    "error_message": str(error),
                },
            )
            return

        with sentry_sdk.isolation_scope() as scope:
            if execution_ctx is not None:
                scope.set_tag("run_id", execution_ctx.run_id)
                scope.set_tag("deployment_id", str(execution_ctx.deployment_id) if execution_ctx.deployment_id is not None else "")
                scope.set_tag("root_deployment_id", str(execution_ctx.root_deployment_id) if execution_ctx.root_deployment_id is not None else "")
                scope.set_tag("span_id", str(span_id))
                scope.set_tag("span_kind", started_state.kind.value)
                scope.set_tag("span_name", started_state.name)
                if execution_ctx.flow_frame is not None:
                    scope.set_tag("flow_name", execution_ctx.flow_frame.name)
                if execution_ctx.task_frame is not None:
                    scope.set_tag("task_name", execution_ctx.task_frame.task_class_name)
            model_name = meta.get("model")
            if isinstance(model_name, str) and model_name:
                scope.set_tag("model", model_name)
            for retry_error in meta.get("retry_errors", []):
                if not isinstance(retry_error, dict):
                    continue
                sentry_sdk.add_breadcrumb(
                    category="retry",
                    level="warning",
                    message=str(retry_error.get("error_message", "retry failure")),
                    data=dict(retry_error),
                )
            sentry_sdk.capture_exception(error)


class SentryLogSink(LogSink):
    """Write third-party warning/error logs as Sentry breadcrumbs."""

    async def on_logs_batch(self, logs: list[LogRecord]) -> None:
        """Convert warning/error dependency logs into breadcrumbs."""
        _ = self
        for log in logs:
            if log.level not in {"WARNING", "ERROR", "CRITICAL"}:
                continue
            if log.logger_name.startswith("ai_pipeline_core"):
                continue
            sentry_sdk.add_breadcrumb(
                category="log",
                level=log.level.lower(),
                message=log.message,
                data={
                    "logger_name": log.logger_name,
                    "event_type": log.event_type,
                    "deployment_id": str(log.deployment_id),
                    "span_id": str(log.span_id),
                    "fields_json": log.fields_json,
                },
            )
