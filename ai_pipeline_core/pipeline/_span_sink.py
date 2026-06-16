"""Span sink protocol and database-backed sink implementation."""

import asyncio
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
from uuid import UUID

from ai_pipeline_core.database import SpanKind, SpanRecord, SpanStatus
from ai_pipeline_core.database._errors import is_transient_db_error
from ai_pipeline_core.database._json_helpers import json_dumps
from ai_pipeline_core.database._protocol import DatabaseWriter
from ai_pipeline_core.pipeline._execution_context import get_execution_context
from ai_pipeline_core.pipeline._span_types import SpanContext, SpanMetrics, SpanSink
from ai_pipeline_core.pipeline._task_runtime import _next_span_version

__all__ = [
    "DatabaseSpanSink",
    "SpanContext",
    "SpanMetrics",
    "SpanSink",
    "flush_pending_terminal_spans",
]

logger = logging.getLogger(__name__)
EMPTY_LOG_SUMMARY = {
    "total": 0,
    "warnings": 0,
    "errors": 0,
    "last_error": "",
}

# --- Terminal-span retry buffer tuning -------------------------------------
# The buffer protects only terminal rows (a self-contained terminal row is all
# a span needs to leave ``running``). It is bounded by both count and bytes so
# a sustained outage can never grow memory without limit; on overflow the
# oldest rows are dropped (FIFO) and recovered later by reconciliation.
_MAX_BUFFER_ROWS = 10_000
_MAX_BUFFER_BYTES = 64 * 1024 * 1024
_DRAIN_BATCH_SIZE = 1_000
_MAX_ROW_ATTEMPTS = 5
_SHA_BYTE_ESTIMATE = 64
_ROW_BYTE_OVERHEAD = 512
_DRAIN_BUDGET_SECONDS = 30.0
_DRAIN_INITIAL_BACKOFF = 0.25
_DRAIN_MAX_BACKOFF = 2.0


@dataclass(slots=True)
class _BufferedSpan:
    """A parked terminal span row awaiting a successful (re)insert."""

    span: SpanRecord
    nbytes: int
    attempts: int = 0


def _strip_heavy_payload(span: SpanRecord) -> SpanRecord:
    """Blank the heavy ZSTD JSON columns; keep structural + doc/blob/meta fields.

    The full-fidelity row is what the hot path attempts to insert; only the
    parked fallback (used when that insert fails) is stripped, so reconciled
    rows keep status, cost, tokens, meta, and document references while losing
    only the large input/output/error/receiver previews.
    """
    return replace(span, receiver_json="", input_json="", output_json="", error_json="")


def _approx_row_bytes(span: SpanRecord) -> int:
    sha_count = (
        len(span.input_document_shas)
        + len(span.output_document_shas)
        + len(span.input_blob_shas)
        + len(span.output_blob_shas)
    )
    return (
        len(span.meta_json)
        + len(span.metrics_json)
        + len(span.error_message)
        + _SHA_BYTE_ESTIMATE * sha_count
        + _ROW_BYTE_OVERHEAD
    )


@dataclass(frozen=True, slots=True)
class _StartedSpanState:
    parent_span_id: UUID | None
    kind: SpanKind
    name: str
    target: str
    started_at: datetime
    started_version: int
    sequence_no: int
    receiver_json: str
    input_json: str
    input_document_shas: tuple[str, ...]
    input_blob_shas: tuple[str, ...]
    label_keys: tuple[str, ...]
    label_values: tuple[str, ...]
    previous_conversation_id: UUID | None


class DatabaseSpanSink:
    """Persist tracked span lifecycle updates into the active database backend."""

    def __init__(self, database: DatabaseWriter) -> None:
        self._database = database
        self._started: dict[UUID, _StartedSpanState] = {}
        # Bounded retry buffer of terminal rows whose insert has not yet
        # succeeded, keyed by span_id (a re-finished span overwrites its own
        # entry). FIFO ordering drives both opportunistic drains and overflow
        # eviction. Mutated only synchronously between awaits, so single-thread
        # asyncio makes it safe without a lock.
        self._buffer: OrderedDict[UUID, _BufferedSpan] = OrderedDict()
        self._buffer_bytes = 0
        self._draining = False
        self._dropped_terminal = 0
        self._poisoned_terminal = 0

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
        _ = input_preview
        execution_ctx = get_execution_context()
        if execution_ctx is None:
            return

        deployment_id = execution_ctx.deployment_id or UUID(int=0)
        root_deployment_id = execution_ctx.root_deployment_id or execution_ctx.deployment_id or UUID(int=0)
        sequence_no = execution_ctx.next_child_sequence(parent_span_id) if parent_span_id is not None else 0
        started_version = _next_span_version()
        previous_conversation_id = _extract_previous_conversation_id(kind, receiver_json)
        is_deployment = kind == SpanKind.DEPLOYMENT
        label_keys = execution_ctx.label_keys if is_deployment else ()
        label_values = execution_ctx.label_values if is_deployment else ()

        self._started[span_id] = _StartedSpanState(
            parent_span_id=parent_span_id,
            kind=kind,
            name=name,
            target=target,
            started_at=started_at,
            started_version=started_version,
            sequence_no=sequence_no,
            receiver_json=receiver_json,
            input_json=input_json,
            input_document_shas=tuple(sorted(input_document_shas)),
            input_blob_shas=tuple(sorted(input_blob_shas)),
            label_keys=label_keys,
            label_values=label_values,
            previous_conversation_id=previous_conversation_id,
        )

        await self._insert_span_safe(
            SpanRecord(
                span_id=span_id,
                parent_span_id=parent_span_id,
                deployment_id=deployment_id,
                root_deployment_id=root_deployment_id,
                run_id=execution_ctx.run_id,
                deployment_name=execution_ctx.deployment_name,
                kind=kind,
                name=name,
                description="",
                status=SpanStatus.RUNNING,
                sequence_no=sequence_no,
                started_at=started_at,
                version=started_version,
                target=target,
                receiver_json=receiver_json,
                input_json=input_json,
                input_document_shas=tuple(sorted(input_document_shas)),
                input_blob_shas=tuple(sorted(input_blob_shas)),
                label_keys=label_keys,
                label_values=label_values,
                previous_conversation_id=previous_conversation_id,
            )
        )

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
        _ = output_preview
        execution_ctx = get_execution_context()
        if execution_ctx is None:
            return

        started_state = self._started.pop(span_id, None)
        if started_state is None:
            started_state = _StartedSpanState(
                parent_span_id=execution_ctx.parent_span_id,
                kind=SpanKind.OPERATION,
                name="",
                target="",
                started_at=ended_at,
                started_version=_next_span_version(),
                sequence_no=0,
                receiver_json="",
                input_json="",
                input_document_shas=(),
                input_blob_shas=(),
                label_keys=(),
                label_values=(),
                previous_conversation_id=None,
            )

        deployment_id = execution_ctx.deployment_id or UUID(int=0)
        root_deployment_id = execution_ctx.root_deployment_id or execution_ctx.deployment_id or UUID(int=0)
        cost_usd = metrics.cost_usd or 0.0
        description = ""
        raw_description = meta.pop("description", "")
        raw_cache_key = meta.pop("cache_key", "")
        raw_status = meta.get("_span_status")
        if isinstance(raw_description, str):
            description = raw_description
        cache_key = raw_cache_key if isinstance(raw_cache_key, str) else ""
        meta_json = json_dumps(_strip_control_meta(meta))
        metrics_json = json_dumps(_metrics_to_json(metrics))
        status = _resolve_finished_status(error=error, raw_status=raw_status)

        await self._insert_span_safe(
            SpanRecord(
                span_id=span_id,
                parent_span_id=started_state.parent_span_id,
                deployment_id=deployment_id,
                root_deployment_id=root_deployment_id,
                run_id=execution_ctx.run_id,
                deployment_name=execution_ctx.deployment_name,
                kind=started_state.kind,
                name=started_state.name,
                description=description,
                status=status,
                sequence_no=started_state.sequence_no,
                started_at=started_state.started_at,
                ended_at=ended_at,
                version=_next_span_version(started_state.started_version),
                cost_usd=cost_usd,
                cache_key=cache_key,
                error_type=type(error).__name__ if error is not None else "",
                error_message=str(error) if error is not None else "",
                input_document_shas=started_state.input_document_shas,
                output_document_shas=tuple(sorted(output_document_shas)),
                target=started_state.target,
                receiver_json=started_state.receiver_json,
                input_json=started_state.input_json,
                output_json=output_json,
                error_json=error_json,
                meta_json=meta_json,
                metrics_json=metrics_json,
                input_blob_shas=started_state.input_blob_shas,
                output_blob_shas=tuple(sorted(output_blob_shas)),
                label_keys=started_state.label_keys,
                label_values=started_state.label_values,
                previous_conversation_id=started_state.previous_conversation_id,
            )
        )

    async def _insert_span_safe(self, span: SpanRecord) -> None:
        # Record-then-write: park the terminal row *before* the await so a
        # swallowed insert failure OR a ``move_on_after`` cancellation still
        # leaves the row buffered for the drain/pump. Running rows are
        # best-effort live-progress and are never buffered (the terminal row is
        # self-contained).
        terminal = span.status != SpanStatus.RUNNING
        if terminal:
            self._park(span)
        execution_ctx = get_execution_context()
        try:
            await self._database.insert_span(span)
        except BaseException as exc:
            if execution_ctx is not None:
                execution_ctx.recording_degraded = True
            self._set_replay_root(span, execution_ctx)
            if _must_reraise_sink_error(exc):
                raise
            logger.warning(
                "Database span insert failed for %s %s: %s. Buffered for retry; "
                "span recording is degraded for this execution.",
                span.kind,
                span.span_id,
                exc,
            )
            return
        if terminal:
            self._unpark(span.span_id)
            # Opportunistic, non-blocking, backlog-only pump: when a successful
            # terminal insert proves the database is reachable again, drain one
            # batch of any rows that piled up during an earlier outage. No-op in
            # steady state (the buffer is empty after each unpark). Only terminal
            # inserts trigger the pump so RUNNING start-row paths never block on
            # backlog drainage — the finish path is already bounded by the 5s
            # move_on_after in track_span.
            await self._pump()
        self._set_replay_root(span, execution_ctx)

    @staticmethod
    def _set_replay_root(span: SpanRecord, execution_ctx: Any | None) -> None:
        if execution_ctx is not None and span.parent_span_id is None and execution_ctx.replay_root_span_id is None:
            execution_ctx.replay_root_span_id = span.span_id

    # --- Retry buffer ------------------------------------------------------

    def _park(self, span: SpanRecord) -> None:
        span_id = span.span_id
        existing = self._buffer.pop(span_id, None)
        if existing is not None:
            self._buffer_bytes -= existing.nbytes
        stripped = _strip_heavy_payload(span)
        nbytes = _approx_row_bytes(stripped)
        self._buffer[span_id] = _BufferedSpan(span=stripped, nbytes=nbytes)
        self._buffer_bytes += nbytes
        while self._buffer and (len(self._buffer) > _MAX_BUFFER_ROWS or self._buffer_bytes > _MAX_BUFFER_BYTES):
            _, dropped = self._buffer.popitem(last=False)
            self._buffer_bytes -= dropped.nbytes
            self._dropped_terminal += 1

    def _unpark(self, span_id: UUID) -> None:
        existing = self._buffer.pop(span_id, None)
        if existing is not None:
            self._buffer_bytes -= existing.nbytes

    def _take_batch(self, limit: int) -> list[tuple[UUID, _BufferedSpan]]:
        batch: list[tuple[UUID, _BufferedSpan]] = []
        while self._buffer and len(batch) < limit:
            span_id, pending = self._buffer.popitem(last=False)
            self._buffer_bytes -= pending.nbytes
            batch.append((span_id, pending))
        return batch

    def _repark(self, batch: list[tuple[UUID, _BufferedSpan]]) -> None:
        # Re-insert at the front (preserve oldest-first), then re-enforce caps.
        for span_id, pending in reversed(batch):
            self._buffer[span_id] = pending
            self._buffer.move_to_end(span_id, last=False)
            self._buffer_bytes += pending.nbytes
        while self._buffer and (len(self._buffer) > _MAX_BUFFER_ROWS or self._buffer_bytes > _MAX_BUFFER_BYTES):
            _, dropped = self._buffer.popitem(last=False)
            self._buffer_bytes -= dropped.nbytes
            self._dropped_terminal += 1

    @staticmethod
    def _degrade() -> None:
        execution_ctx = get_execution_context()
        if execution_ctx is not None:
            execution_ctx.recording_degraded = True

    def _quarantine(self, span_id: UUID, pending: _BufferedSpan, exc: BaseException) -> None:
        self._poisoned_terminal += 1
        self._degrade()
        logger.warning(
            "Quarantining terminal span %s (kind=%s, version=%d) after %d failed insert attempts: %s. "
            "The terminal row is dropped; reconciliation will mark this span lost.",
            span_id,
            pending.span.kind,
            pending.span.version,
            pending.attempts,
            exc,
        )

    async def _resolve_batch(self, batch: list[tuple[UUID, _BufferedSpan]]) -> None:
        """Insert a taken batch; isolate poison rows via split + attempt cap.

        Every failing batch increments every member's attempt counter so the cap
        is always reachable. Transient errors keep the batch together for
        efficiency (likely the whole batch will succeed on the next pump once the
        DB recovers). Once any member exceeds the attempt cap — indicating a
        persistently failing batch, likely containing a poison row — the batch is
        force-split regardless of error classification to isolate the offender.
        """
        if not batch:
            return
        spans = [pending.span for _, pending in batch]
        try:
            await self._database.insert_span_batch(spans)
        except BaseException as exc:
            if _must_reraise_sink_error(exc):
                self._repark(batch)
                raise
            for _, pending in batch:
                pending.attempts += 1
            stale = any(p.attempts >= _MAX_ROW_ATTEMPTS for _, p in batch)
            if is_transient_db_error(exc) and not stale:
                self._repark(batch)
                self._degrade()
                return
            if len(batch) == 1:
                span_id, pending = batch[0]
                if pending.attempts >= _MAX_ROW_ATTEMPTS:
                    self._quarantine(span_id, pending, exc)
                else:
                    self._repark(batch)
                    self._degrade()
                return
            mid = len(batch) // 2
            await self._resolve_batch(batch[:mid])
            await self._resolve_batch(batch[mid:])

    async def _pump(self) -> None:
        if not self._buffer or self._draining:
            return
        self._draining = True
        try:
            await self._resolve_batch(self._take_batch(_DRAIN_BATCH_SIZE))
        finally:
            self._draining = False

    async def flush_pending(self, *, budget_s: float = _DRAIN_BUDGET_SECONDS) -> None:
        """Drain buffered terminal rows within a time budget (end-of-run/shutdown).

        Bounded so shutdown can never hang: every inner batch insert is wrapped
        in an ``asyncio.timeout`` using the remaining budget, so a single slow
        ClickHouse request cannot exceed the overall budget. If the database
        stays unreachable the remaining rows are left for reconciliation.
        """
        if not self._buffer or self._draining:
            return
        self._draining = True
        try:
            deadline = time.monotonic() + budget_s
            backoff = _DRAIN_INITIAL_BACKOFF
            while self._buffer and time.monotonic() < deadline:
                before = len(self._buffer)
                remaining = max(0.1, deadline - time.monotonic())
                try:
                    async with asyncio.timeout(remaining):
                        await self._resolve_batch(self._take_batch(_DRAIN_BATCH_SIZE))
                except TimeoutError:
                    break
                if len(self._buffer) >= before:
                    sleep_s = min(backoff, _DRAIN_MAX_BACKOFF, deadline - time.monotonic())
                    if sleep_s <= 0:
                        break
                    await asyncio.sleep(sleep_s)
                    backoff *= 2
                else:
                    backoff = _DRAIN_INITIAL_BACKOFF
        finally:
            self._draining = False


async def flush_pending_terminal_spans(
    span_sinks: Sequence[SpanSink],
    *,
    budget_s: float = _DRAIN_BUDGET_SECONDS,
) -> None:
    """Drain every ``DatabaseSpanSink``'s terminal-row buffer before shutdown.

    Wired at each deployment/debug finalization boundary so a transient database
    outage during the run does not leave finished spans stuck ``running``.
    """
    for sink in span_sinks:
        if isinstance(sink, DatabaseSpanSink):
            await sink.flush_pending(budget_s=budget_s)


def _must_reraise_sink_error(error: BaseException) -> bool:
    return isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit))


def _metrics_to_json(metrics: SpanMetrics) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "time_taken_ms": metrics.time_taken_ms,
        "log_summary": metrics.log_summary,
    }
    optional_fields = (
        ("tokens_input", metrics.tokens_input),
        ("tokens_output", metrics.tokens_output),
        ("tokens_cache_read", metrics.tokens_cache_read),
        ("tokens_reasoning", metrics.tokens_reasoning),
        ("cost_usd", metrics.cost_usd),
        ("first_token_ms", metrics.first_token_ms),
    )
    for field_name, value in optional_fields:
        if value is not None:
            payload.update({field_name: value})
    return payload


def _extract_previous_conversation_id(kind: SpanKind, receiver_json: str) -> UUID | None:
    if kind != SpanKind.CONVERSATION or not receiver_json:
        return None
    receiver = _parse_receiver_json(receiver_json)
    if receiver is None:
        return None
    conversation_id = _extract_conversation_id(receiver)
    if conversation_id is None:
        return None
    try:
        return UUID(conversation_id)
    except ValueError:
        return None


def _parse_receiver_json(receiver_json: str) -> dict[str, Any] | None:
    try:
        receiver = json.loads(receiver_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(receiver, dict) or receiver.get("mode") != "decoded_state":
        return None
    return receiver


def _extract_conversation_id(receiver: dict[str, Any]) -> str | None:
    value = receiver.get("value")
    if not isinstance(value, dict):
        return None
    data = value.get("data")
    if not isinstance(data, dict):
        return None
    conversation_id = data.get("_conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        return None
    return conversation_id


def _resolve_finished_status(*, error: BaseException | None, raw_status: Any) -> SpanStatus:
    if error is not None:
        return SpanStatus.FAILED
    if isinstance(raw_status, SpanStatus):
        return raw_status
    if isinstance(raw_status, str) and raw_status:
        return SpanStatus(raw_status)
    return SpanStatus.COMPLETED


def _strip_control_meta(meta: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in meta.items() if not key.startswith("_")}
