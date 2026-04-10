"""Sentry span and log sinks."""

import contextlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import sentry_sdk
from ai_pipeline_core.database import SpanKind
from ai_pipeline_core.pipeline._execution_context import get_execution_context
from ai_pipeline_core.pipeline._span_types import SpanMetrics

__all__ = ["SentrySpanSink"]

_ISSUE_CAPTURE_KINDS = frozenset({SpanKind.TASK, SpanKind.FLOW, SpanKind.DEPLOYMENT})


def _replay_retry_breadcrumbs(scope: Any, meta: dict[str, Any]) -> None:
    """Attach retry/attempt-failure metadata from the span as Sentry breadcrumbs."""
    for retry_error in meta.get("retry_errors", []):
        if not isinstance(retry_error, dict):
            continue
        will_retry = bool(retry_error.get("will_retry", False))
        message = str(retry_error.get("error_message", "attempt failed"))
        if will_retry:
            message = f"{message} (retrying)"
        scope.add_breadcrumb(
            category="retry",
            level="warning",
            message=message,
            data=dict(retry_error),
        )


@dataclass(frozen=True, slots=True)
class _StartedSpanState:
    kind: SpanKind
    name: str


class SentrySpanSink:
    """Capture terminal span failures in Sentry."""

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
        if not isinstance(error, Exception):
            return
        if started_state.kind == SpanKind.ATTEMPT:
            return
        if started_state.kind not in _ISSUE_CAPTURE_KINDS:
            return
        if getattr(error, "_sentry_captured", False):
            return
        self._captured.add(span_id)
        execution_ctx = get_execution_context()

        with sentry_sdk.new_scope() as scope:
            scope.set_tag("span_id", str(span_id))
            scope.set_tag("span_kind", started_state.kind.value)
            scope.set_tag("span_name", started_state.name)
            if execution_ctx is not None:
                if execution_ctx.flow_frame is not None:
                    scope.set_tag("flow_name", execution_ctx.flow_frame.name)
                    scope.set_tag("flow_step", str(execution_ctx.flow_frame.step))
                if execution_ctx.task_frame is not None:
                    scope.set_tag("task_name", execution_ctx.task_frame.task_class_name)
            model_name = meta.get("model")
            if isinstance(model_name, str) and model_name:
                scope.set_tag("model", model_name)
            _replay_retry_breadcrumbs(scope, meta)
            sentry_sdk.capture_exception(error)
            with contextlib.suppress(AttributeError, TypeError):
                object.__setattr__(error, "_sentry_captured", True)  # noqa: PLC2801 — bypass potential custom __setattr__ on exception subclasses
