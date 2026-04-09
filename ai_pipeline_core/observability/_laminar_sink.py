"""Laminar span sink integration."""

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from lmnr import Laminar
from lmnr.opentelemetry_lib.tracing.instruments import Instruments
from opentelemetry.trace import Status, StatusCode

from ai_pipeline_core.database import SpanKind
from ai_pipeline_core.pipeline._span_sink import SpanMetrics
from ai_pipeline_core.settings import Settings

__all__ = ["LaminarSpanSink", "_reset_for_testing"]

logger = logging.getLogger(__name__)
LaminarSpanType = Literal["DEFAULT", "LLM", "TOOL"]
_LAMINAR_DEFAULT_SPAN_TYPE: LaminarSpanType = "DEFAULT"
_LAMINAR_LLM_SPAN_TYPE: LaminarSpanType = "LLM"
_LAMINAR_TOOL_SPAN_TYPE: LaminarSpanType = "TOOL"
_LAMINAR_EXCEPTIONS = (AttributeError, OSError, RuntimeError, TypeError, ValueError)


@dataclass(slots=True)
class _OpenLaminarSpan:
    span: Any
    context: Any
    kind: SpanKind


class LaminarSpanSink:
    """Mirror tracked framework spans into Laminar."""

    _initialize_lock = threading.Lock()
    _initialized = False
    _initialization_failed = False
    _initialized_project_api_key: str | None = None

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._open_spans: dict[UUID, _OpenLaminarSpan] = {}
        self._input_previews: dict[UUID, Any] = {}

    def _ensure_initialized(self) -> bool:
        configured_api_key = self._settings.lmnr_project_api_key
        if not configured_api_key:
            return False
        ready = self._initialization_state_allows_export(configured_api_key)
        if ready is not None:
            return ready
        with type(self)._initialize_lock:
            ready = self._initialization_state_allows_export(configured_api_key)
            if ready is not None:
                return ready
            return self._initialize_laminar(configured_api_key)

    async def on_span_started(
        self,
        *,
        span_id: UUID,
        parent_span_id: UUID | None,
        kind: SpanKind,
        name: str,
        target: str,
        started_at: datetime,
        input_preview: Any | None,
        **extra: Any,
    ) -> None:
        _ = (started_at, extra)
        if not self._ensure_initialized():
            return

        try:
            parent_ctx = None
            if parent_span_id is not None and parent_span_id in self._open_spans:
                parent_ctx = self._open_spans[parent_span_id].context
            span = Laminar.start_span(
                name=name,
                span_type=_laminar_type(kind),
                parent_span_context=parent_ctx,
                input=input_preview,
                attributes={
                    "ai_pipeline.span_kind": kind.value,
                    "ai_pipeline.target": target,
                },
            )
        except _LAMINAR_EXCEPTIONS as exc:
            logger.warning("Laminar span start failed for %s '%s': %s", kind.value, name, exc)
            return

        try:
            span_context = _get_span_context(span)
        except _LAMINAR_EXCEPTIONS as exc:
            logger.warning("Laminar span context capture failed for %s '%s': %s", kind.value, name, exc)
            try:
                span.end()
            except _LAMINAR_EXCEPTIONS as end_exc:
                logger.warning("Laminar span end failed after context capture error for %s '%s': %s", kind.value, name, end_exc)
            return

        self._open_spans[span_id] = _OpenLaminarSpan(
            span=span,
            context=span_context,
            kind=kind,
        )
        self._input_previews[span_id] = input_preview

    async def on_span_finished(
        self,
        *,
        span_id: UUID,
        ended_at: datetime,
        output_preview: Any | None,
        error: BaseException | None,
        metrics: SpanMetrics,
        meta: dict[str, Any],
        **extra: Any,
    ) -> None:
        _ = (ended_at, extra)
        open_span = self._open_spans.pop(span_id, None)
        input_preview = self._input_previews.pop(span_id, None)
        if open_span is None:
            return

        attributes: dict[str, Any] = {}
        if metrics.cost_usd is not None:
            attributes["gen_ai.usage.cost"] = metrics.cost_usd
        if open_span.kind == SpanKind.LLM_ROUND:
            attributes.update(_llm_gen_ai_attrs(meta, metrics, input_preview, output_preview))
        if error is not None:
            attributes["ai_pipeline.error_type"] = type(error).__name__
            attributes["ai_pipeline.error_message"] = str(error)
            try:
                open_span.span.set_status(Status(StatusCode.ERROR, description=f"{type(error).__name__}: {error}"))
            except _LAMINAR_EXCEPTIONS as exc:
                logger.warning("Laminar span status update failed for span %s: %s", span_id, exc)
            try:
                open_span.span.record_exception(error)
            except _LAMINAR_EXCEPTIONS as exc:
                logger.warning("Laminar exception recording failed for span %s: %s", span_id, exc)

        if attributes:
            try:
                open_span.span.set_attributes(_clean_attributes(attributes))
            except _LAMINAR_EXCEPTIONS as exc:
                logger.warning("Laminar attribute export failed for span %s: %s", span_id, exc)

        if output_preview is not None:
            try:
                open_span.span.set_output(output_preview)
            except _LAMINAR_EXCEPTIONS as exc:
                logger.warning("Laminar output export failed for span %s: %s", span_id, exc)

        try:
            open_span.span.end()
        except _LAMINAR_EXCEPTIONS as exc:
            logger.warning("Laminar span end failed for span %s: %s", span_id, exc)

    def _initialization_state_allows_export(self, configured_api_key: str) -> bool | None:
        if type(self)._initialized:
            if type(self)._initialized_project_api_key == configured_api_key:
                return True
            self._warn_project_switch()
            return False
        if type(self)._initialization_failed:
            if type(self)._initialized_project_api_key not in {None, configured_api_key}:
                logger.error(
                    "Laminar initialization previously failed for a different LMNR_PROJECT_API_KEY. This execution will skip Laminar export for key isolation."
                )
            return False
        return None

    def _initialize_laminar(self, configured_api_key: str) -> bool:
        init_error: str | None = None
        try:
            Laminar.initialize(
                project_api_key=configured_api_key,
                disabled_instruments={Instruments.OPENAI, Instruments.LITELLM},
            )
        except _LAMINAR_EXCEPTIONS as exc:
            init_error = str(exc)
            type(self)._initialization_failed = True
            type(self)._initialized_project_api_key = configured_api_key

        if init_error is not None:
            logger.error(
                "Laminar initialization failed. Span export is disabled for this process. Unset LMNR_PROJECT_API_KEY to disable Laminar cleanly: %s",
                init_error,
            )
            return False

        type(self)._initialized = True
        type(self)._initialized_project_api_key = configured_api_key
        return True

    @staticmethod
    def _warn_project_switch() -> None:
        logger.error(
            "Laminar was already initialized for a different LMNR_PROJECT_API_KEY. "
            "Laminar projects cannot be switched after process initialization. "
            "This execution will skip Laminar export instead of sending spans to the wrong project."
        )


def _laminar_type(kind: SpanKind) -> LaminarSpanType:
    if kind == SpanKind.LLM_ROUND:
        return _LAMINAR_LLM_SPAN_TYPE
    if kind == SpanKind.TOOL_CALL:
        return _LAMINAR_TOOL_SPAN_TYPE
    return _LAMINAR_DEFAULT_SPAN_TYPE


def _get_span_context(span: Any) -> Any:
    getter = getattr(span, "get_laminar_span_context", None)
    if callable(getter):
        return getter()
    fallback = getattr(span, "get_span_context", None)
    if callable(fallback):
        return fallback()
    raise AttributeError(
        "Laminar span object does not expose get_laminar_span_context() or get_span_context(). Upgrade lmnr or adapt LaminarSpanSink to the installed SDK API."
    )


def _llm_gen_ai_attrs(
    meta: dict[str, Any],
    metrics: SpanMetrics,
    input_preview: Any | None,
    output_preview: Any | None,
) -> dict[str, Any]:
    attrs: dict[str, Any] = {
        "gen_ai.system": "litellm",
        "gen_ai.usage.input_tokens": metrics.tokens_input,
        "gen_ai.usage.output_tokens": metrics.tokens_output,
        "gen_ai.usage.cache_read_input_tokens": metrics.tokens_cache_read,
        "gen_ai.usage.reasoning_tokens": metrics.tokens_reasoning,
        "gen_ai.input.messages": json.dumps(input_preview, default=str) if input_preview is not None else None,
        "gen_ai.output.messages": json.dumps(output_preview, default=str) if output_preview is not None else None,
    }

    model = meta.get("model")
    response_id = meta.get("response_id")
    if model:
        attrs["gen_ai.request.model"] = model
        attrs["gen_ai.response.model"] = model
    if response_id:
        attrs["gen_ai.response.id"] = response_id
    return attrs


def _clean_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in attributes.items() if value is not None}


def _reset_for_testing() -> None:
    """Reset process-global Laminar initialization state for tests."""
    LaminarSpanSink._initialized = False
    LaminarSpanSink._initialization_failed = False
    LaminarSpanSink._initialized_project_api_key = None
