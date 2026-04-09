"""Unified async context manager for tracked execution spans."""

import asyncio
import logging
import traceback
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import ExitStack, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid7

import anyio
from pydantic import BaseModel

from ai_pipeline_core._codec import SerializedError, UniversalCodec
from ai_pipeline_core.database import SpanKind
from ai_pipeline_core.database._documents import document_to_blobs, document_to_record
from ai_pipeline_core.database._json_helpers import json_dumps
from ai_pipeline_core.database._protocol import DatabaseWriter
from ai_pipeline_core.database._types import _BlobRecord
from ai_pipeline_core.documents import Document
from ai_pipeline_core.documents._hashing import compute_content_sha256
from ai_pipeline_core.pipeline._execution_context import (
    ExecutionContext,
    get_execution_context,
    set_execution_context,
)
from ai_pipeline_core.pipeline._span_sink import (
    EMPTY_LOG_SUMMARY,
    DatabaseSpanSink,
    SpanContext,
    SpanSink,
    _must_reraise_sink_error,
)

__all__ = ["get_current_span_context", "track_span"]

logger = logging.getLogger(__name__)
_UNSET = object()
_current_span_context: ContextVar[SpanContext | None] = ContextVar("_current_span_context", default=None)
_KIND_TO_LOG_PREFIX: dict[SpanKind, str] = {
    SpanKind.DEPLOYMENT: "run",
    SpanKind.FLOW: "flow",
    SpanKind.TASK: "task",
    SpanKind.CONVERSATION: "conversation",
    SpanKind.LLM_ROUND: "llm",
    SpanKind.TOOL_CALL: "tool",
    SpanKind.ATTEMPT: "attempt",
    SpanKind.OPERATION: "operation",
}
_SHUTDOWN_TIMEOUT_SECONDS = 5.0


def get_current_span_context() -> SpanContext | None:
    """Return the innermost active SpanContext, or None if not inside a tracked span."""
    return _current_span_context.get()


class SpanArtifactPersistenceError(RuntimeError):
    """Raised when span artifact persistence fails before a database-backed span write."""


@dataclass(frozen=True, slots=True)
class _EncodedSpanStart:
    receiver_json: str
    input_json: str
    document_shas: frozenset[str]
    blob_shas: frozenset[str]
    active_sinks: tuple[SpanSink, ...]


@dataclass(frozen=True, slots=True)
class _EncodedSpanFinish:
    output_json: str
    error_json: str
    output_document_shas: frozenset[str]
    output_blob_shas: frozenset[str]
    active_sinks: tuple[SpanSink, ...]


@asynccontextmanager
async def track_span(  # noqa: PLR0914 - span lifecycle orchestration needs several local values
    kind: SpanKind,
    name: str,
    target: str,
    *,
    sinks: Sequence[SpanSink],
    span_id: UUID | None = None,
    parent_span_id: UUID | None = None,
    encode_receiver: dict[str, Any] | None = None,
    encode_input: Any = _UNSET,
    db: DatabaseWriter | None = None,
    input_preview: Any | None = None,
) -> AsyncIterator[SpanContext]:
    """Track one span lifecycle and dispatch it to every configured sink.

    Yields:
        SpanContext: Mutable span state for previews, metadata, metrics, and output.
    """
    execution_ctx = get_execution_context()
    span_id = span_id or uuid7()
    effective_parent_span_id = parent_span_id
    if effective_parent_span_id is None and execution_ctx is not None and (execution_ctx.current_span_id or execution_ctx.span_id) != span_id:
        effective_parent_span_id = execution_ctx.current_span_id or execution_ctx.span_id
    started_at = datetime.now(UTC)
    context = SpanContext(
        span_id=span_id,
        parent_span_id=effective_parent_span_id,
        input_preview=input_preview,
    )
    codec = UniversalCodec()
    start_payload = await _prepare_start_payload(
        codec=codec,
        encode_receiver=encode_receiver,
        encode_input=encode_input,
        db=db or (execution_ctx.database if execution_ctx is not None else None),
        execution_ctx=execution_ctx,
        kind=kind,
        name=name,
        sinks=tuple(sinks),
    )

    span_execution_ctx = (
        execution_ctx.with_span(
            span_id,
            parent_span_id=effective_parent_span_id,
            span_kind=kind.value,
            span_name=name,
            span_target=target,
        )
        if execution_ctx is not None
        else None
    )

    error: BaseException | None = None
    span_ctx_token = _current_span_context.set(context)
    with ExitStack() as stack:
        if span_execution_ctx is not None:
            stack.enter_context(set_execution_context(span_execution_ctx))

        await _notify_sinks(
            start_payload.active_sinks,
            "on_span_started",
            span_id=span_id,
            parent_span_id=effective_parent_span_id,
            kind=kind,
            name=name,
            target=target,
            started_at=started_at,
            receiver_json=start_payload.receiver_json,
            input_json=start_payload.input_json,
            input_document_shas=start_payload.document_shas,
            input_blob_shas=start_payload.blob_shas,
            input_preview=context.input_preview,
        )
        logger.info(
            "%s.started: %s",
            _KIND_TO_LOG_PREFIX[kind],
            name,
            extra={
                "category": "lifecycle",
                "lifecycle": True,
                "event_type": f"{_KIND_TO_LOG_PREFIX[kind]}.started",
                "span_kind": kind.value,
                "span_name": name,
                "span_target": target,
            },
        )

        try:
            yield context
        except BaseException as exc:
            error = exc
            raise
        finally:
            _current_span_context.reset(span_ctx_token)
            ended_at = datetime.now(UTC)
            finish_payload = await _prepare_finish_payload(
                codec=codec,
                output_value=context._output_value,
                has_output_value=context._has_output_value,
                error=error,
                db=db or (execution_ctx.database if execution_ctx is not None else None),
                execution_ctx=execution_ctx,
                kind=kind,
                name=name,
                sinks=start_payload.active_sinks,
            )

            meta = dict(context._meta)
            if context._retry_errors:
                meta["retry_count"] = len(context._retry_errors)
                meta["retry_errors"] = context._retry_errors
            if context._status is not None:
                meta["_span_status"] = context._status
            terminal_suffix = _terminal_suffix(raw_status=context._status, error=error)
            _log_terminal_event(kind=kind, name=name, target=target, suffix=terminal_suffix, error=error)
            log_summary = _consume_log_summary(span_execution_ctx or execution_ctx, span_id)
            metrics = context._build_metrics(ended_at=ended_at, started_at=started_at, log_summary=log_summary)
            if metrics.cost_usd and execution_ctx is not None:
                execution_ctx._recording_state.total_cost_usd += metrics.cost_usd
            is_shutdown = error is not None and not isinstance(error, Exception)
            with anyio.move_on_after(_SHUTDOWN_TIMEOUT_SECONDS, shield=True):
                await _notify_sinks(
                    finish_payload.active_sinks,
                    "on_span_finished",
                    suppress_shutdown_errors=is_shutdown,
                    span_id=span_id,
                    ended_at=ended_at,
                    output_json=finish_payload.output_json,
                    error_json=finish_payload.error_json,
                    output_document_shas=finish_payload.output_document_shas,
                    output_blob_shas=finish_payload.output_blob_shas,
                    output_preview=context.output_preview,
                    error=error,
                    metrics=metrics,
                    meta=meta,
                )


def _mark_recording_degraded() -> None:
    execution_ctx = get_execution_context()
    if execution_ctx is not None:
        execution_ctx.recording_degraded = True


def _handle_sink_error(method_name: str, error: BaseException, *, suppress_shutdown_errors: bool) -> None:
    _mark_recording_degraded()
    if suppress_shutdown_errors:
        logger.warning("Span sink callback %s failed during shutdown: %s", method_name, error)
        return
    if _must_reraise_sink_error(error):
        raise error
    logger.warning("Span sink callback %s failed: %s", method_name, error)


async def _notify_sinks(
    sinks: tuple[SpanSink, ...],
    method_name: str,
    *,
    suppress_shutdown_errors: bool = False,
    **kwargs: Any,
) -> None:
    if not sinks:
        return
    try:
        results = await asyncio.gather(
            *(getattr(sink, method_name)(**kwargs) for sink in sinks),
            return_exceptions=True,
        )
    except asyncio.CancelledError as exc:
        _handle_sink_error(method_name, exc, suppress_shutdown_errors=suppress_shutdown_errors)
        return
    except (KeyboardInterrupt, SystemExit) as exc:
        _handle_sink_error(method_name, exc, suppress_shutdown_errors=suppress_shutdown_errors)
        return
    for result in results:
        if isinstance(result, BaseException):
            _handle_sink_error(method_name, result, suppress_shutdown_errors=suppress_shutdown_errors)


def _terminal_suffix(*, raw_status: str | None, error: BaseException | None) -> str:
    if isinstance(raw_status, str) and raw_status in {"cached", "skipped", "completed"}:
        return raw_status
    if raw_status == "failed":
        return "failed"
    if error is not None:
        if isinstance(error, asyncio.CancelledError):
            return "cancelled"
        return "failed"
    return "completed"


def _log_terminal_event(
    *,
    kind: SpanKind,
    name: str,
    target: str,
    suffix: str,
    error: BaseException | None,
) -> None:
    prefix = _KIND_TO_LOG_PREFIX[kind]
    event_type = f"{prefix}.{suffix}"
    extra = {
        "category": "lifecycle",
        "lifecycle": True,
        "event_type": event_type,
        "span_kind": kind.value,
        "span_name": name,
        "span_target": target,
    }
    if suffix == "failed":
        log_method = logger.warning if kind == SpanKind.ATTEMPT else logger.error
        log_method("%s.%s: %s", prefix, suffix, name, extra=extra, exc_info=error)
        return
    if suffix == "cancelled":
        logger.warning("%s.%s: %s", prefix, suffix, name, extra=extra, exc_info=error)
        return
    logger.info("%s.%s: %s", prefix, suffix, name, extra=extra)


def _consume_log_summary(execution_ctx: Any, span_id: UUID) -> dict[str, Any]:
    if execution_ctx is None or execution_ctx.log_buffer is None:
        return dict(EMPTY_LOG_SUMMARY)
    return execution_ctx.log_buffer.consume_summary(span_id)


def _encode_receiver(
    codec: UniversalCodec,
    receiver_payload: dict[str, Any] | None,
) -> tuple[str, frozenset[str], frozenset[str]]:
    if receiver_payload is None:
        return "", frozenset(), frozenset()
    mode = receiver_payload.get("mode")
    if not isinstance(mode, str):
        raise TypeError(
            "encode_receiver must be {'mode': 'constructor_args'|'decoded_state'|'tool_ref', 'value': ...}. "
            "Set the receiver mode explicitly so replay can reconstruct the callable."
        )
    encoded_value = codec.encode(receiver_payload.get("value"))
    return (
        json_dumps({"mode": mode, "value": encoded_value.value}),
        encoded_value.document_shas,
        encoded_value.blob_shas,
    )


def _encode_value(
    codec: UniversalCodec,
    value: Any,
) -> tuple[str, frozenset[str], frozenset[str]]:
    if value is _UNSET:
        return "", frozenset(), frozenset()
    encoded = codec.encode(value)
    return json_dumps(encoded.value), encoded.document_shas, encoded.blob_shas


@dataclass(slots=True)
class _CollectedArtifacts:
    documents: dict[str, Document]
    blobs: dict[str, bytes]


def _collect_artifacts(*values: Any) -> _CollectedArtifacts:
    documents: dict[str, Document] = {}
    blobs: dict[str, bytes] = {}
    seen_ids: set[int] = set()
    for value in values:
        _walk_artifacts(value, documents=documents, blobs=blobs, seen_ids=seen_ids)
    return _CollectedArtifacts(documents=documents, blobs=blobs)


def _walk_artifacts(
    value: Any,
    *,
    documents: dict[str, Document],
    blobs: dict[str, bytes],
    seen_ids: set[int],
) -> None:
    if isinstance(value, bytes):
        blobs.setdefault(compute_content_sha256(value), value)
        return
    if isinstance(value, Document):
        if value.sha256 in documents:
            return
        documents[value.sha256] = value
        return
    if _is_scalar_artifact(value):
        return

    object_id = id(value)
    if object_id in seen_ids:
        return
    seen_ids.add(object_id)
    try:
        codec_state = getattr(value, "__codec_state__", None)
        if callable(codec_state):
            next_value = codec_state()
            _walk_artifacts(next_value, documents=documents, blobs=blobs, seen_ids=seen_ids)
        elif not _walk_model_artifacts(value, documents=documents, blobs=blobs, seen_ids=seen_ids):
            if not _walk_mapping_artifacts(value, documents=documents, blobs=blobs, seen_ids=seen_ids):
                _walk_sequence_artifacts(value, documents=documents, blobs=blobs, seen_ids=seen_ids)
    finally:
        seen_ids.discard(object_id)


def _filter_new_artifacts(
    artifacts: _CollectedArtifacts,
    execution_ctx: ExecutionContext | None,
) -> tuple[dict[str, _BlobRecord], list[Any]]:
    """Filter artifacts against already-persisted SHAs, return (blob_records, document_records)."""
    persisted_doc_shas = execution_ctx._recording_state.persisted_document_shas if execution_ctx is not None else None
    persisted_blob_shas = execution_ctx._recording_state.persisted_blob_shas if execution_ctx is not None else None

    blob_records: dict[str, _BlobRecord] = {}
    for sha256, content in artifacts.blobs.items():
        if persisted_blob_shas is not None and sha256 in persisted_blob_shas:
            continue
        blob_records[sha256] = _BlobRecord(content_sha256=sha256, content=content)

    document_records = []
    for document in artifacts.documents.values():
        if persisted_doc_shas is not None and document.sha256 in persisted_doc_shas:
            continue
        for blob in document_to_blobs(document):
            if persisted_blob_shas is None or blob.content_sha256 not in persisted_blob_shas:
                blob_records.setdefault(blob.content_sha256, blob)
        document_records.append(document_to_record(document))

    return blob_records, document_records


async def _persist_artifacts(database: DatabaseWriter | None, artifacts: _CollectedArtifacts, execution_ctx: ExecutionContext | None = None) -> None:
    if database is None:
        return

    blob_records, document_records = _filter_new_artifacts(artifacts, execution_ctx)

    try:
        if blob_records:
            await database.save_blob_batch(list(blob_records.values()))
        if document_records:
            await database.save_document_batch(document_records)
    except Exception as exc:
        raise SpanArtifactPersistenceError(str(exc)) from exc

    if execution_ctx is not None:
        execution_ctx._recording_state.persisted_blob_shas.update(blob_records)
        for doc in artifacts.documents.values():
            execution_ctx._recording_state.persisted_document_shas.add(doc.sha256)


def _receiver_value(receiver_payload: dict[str, Any] | None) -> Any:
    if receiver_payload is None:
        return _UNSET
    return receiver_payload.get("value", _UNSET)


def _without_database_sinks(sinks: tuple[SpanSink, ...]) -> tuple[SpanSink, ...]:
    return tuple(sink for sink in sinks if not isinstance(sink, DatabaseSpanSink))


async def _prepare_start_payload(
    *,
    codec: UniversalCodec,
    encode_receiver: dict[str, Any] | None,
    encode_input: Any,
    db: DatabaseWriter | None,
    execution_ctx: Any,
    kind: SpanKind,
    name: str,
    sinks: tuple[SpanSink, ...],
) -> _EncodedSpanStart:
    receiver_json, receiver_document_shas, receiver_blob_shas = _encode_receiver(codec, encode_receiver)
    input_json, input_document_shas, input_blob_shas = _encode_value(codec, encode_input)
    active_sinks = await _persist_for_span_boundary(
        db=db,
        execution_ctx=execution_ctx,
        kind=kind,
        name=name,
        sinks=sinks,
        artifacts=_collect_artifacts(_receiver_value(encode_receiver), encode_input),
        stage="input",
    )
    return _EncodedSpanStart(
        receiver_json=receiver_json,
        input_json=input_json,
        document_shas=frozenset((*receiver_document_shas, *input_document_shas)),
        blob_shas=frozenset((*receiver_blob_shas, *input_blob_shas)),
        active_sinks=active_sinks,
    )


async def _prepare_finish_payload(
    *,
    codec: UniversalCodec,
    output_value: Any,
    has_output_value: bool,
    error: BaseException | None,
    db: DatabaseWriter | None,
    execution_ctx: Any,
    kind: SpanKind,
    name: str,
    sinks: tuple[SpanSink, ...],
) -> _EncodedSpanFinish:
    if error is not None or not has_output_value:
        return _EncodedSpanFinish(
            output_json="",
            error_json=_build_error_json(error),
            output_document_shas=frozenset(),
            output_blob_shas=frozenset(),
            active_sinks=sinks,
        )

    output_json, output_document_shas, output_blob_shas = _encode_value(codec, output_value)
    active_sinks = await _persist_for_span_boundary(
        db=db,
        execution_ctx=execution_ctx,
        kind=kind,
        name=name,
        sinks=sinks,
        artifacts=_collect_artifacts(output_value),
        stage="output",
    )
    if active_sinks != sinks:
        output_json = ""
        output_document_shas = frozenset()
        output_blob_shas = frozenset()
    return _EncodedSpanFinish(
        output_json=output_json,
        error_json="",
        output_document_shas=output_document_shas,
        output_blob_shas=output_blob_shas,
        active_sinks=active_sinks,
    )


async def _persist_for_span_boundary(
    *,
    db: DatabaseWriter | None,
    execution_ctx: Any,
    kind: SpanKind,
    name: str,
    sinks: tuple[SpanSink, ...],
    artifacts: _CollectedArtifacts,
    stage: str,
) -> tuple[SpanSink, ...]:
    try:
        await _persist_artifacts(db, artifacts, execution_ctx)
    except SpanArtifactPersistenceError as exc:
        if execution_ctx is not None:
            execution_ctx.recording_degraded = True
        logger.warning(
            "Span %s artifact persistence failed for %s '%s': %s. Database-backed span recording is skipped for this span.",
            stage,
            kind,
            name,
            exc,
        )
        return _without_database_sinks(sinks)
    return sinks


def _build_error_json(error: BaseException | None) -> str:
    if error is None:
        return ""
    error_payload = SerializedError(
        error_class_path=f"{type(error).__module__}:{type(error).__qualname__}",
        type_name=type(error).__name__,
        message=str(error),
        traceback_text="".join(traceback.format_exception(type(error), error, error.__traceback__)),
    )
    return json_dumps(error_payload.model_dump(mode="json"))


def _is_scalar_artifact(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool, UUID, datetime, Enum, type))


def _walk_model_artifacts(
    value: Any,
    *,
    documents: dict[str, Document],
    blobs: dict[str, bytes],
    seen_ids: set[int],
) -> bool:
    if not isinstance(value, BaseModel):
        return False
    for field_name in type(value).model_fields:
        _walk_artifacts(getattr(value, field_name), documents=documents, blobs=blobs, seen_ids=seen_ids)
    return True


def _walk_mapping_artifacts(
    value: Any,
    *,
    documents: dict[str, Document],
    blobs: dict[str, bytes],
    seen_ids: set[int],
) -> bool:
    if not isinstance(value, Mapping):
        return False
    for item in value.values():
        _walk_artifacts(item, documents=documents, blobs=blobs, seen_ids=seen_ids)
    return True


def _walk_sequence_artifacts(
    value: Any,
    *,
    documents: dict[str, Document],
    blobs: dict[str, bytes],
    seen_ids: set[int],
) -> None:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return
    for item in value:
        _walk_artifacts(item, documents=documents, blobs=blobs, seen_ids=seen_ids)
