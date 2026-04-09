"""Reconstruct lifecycle events from database span trees."""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from ai_pipeline_core._lifecycle_events import DocumentRef, TaskCompletedEvent, TaskFailedEvent, TaskStartedEvent
from ai_pipeline_core.database._json_helpers import parse_json_object
from ai_pipeline_core.database._protocol import DatabaseReader
from ai_pipeline_core.database._types import DocumentRecord, SpanKind, SpanRecord, SpanStatus

from ._event_serialization import event_to_payload
from ._types import (
    ErrorCode,
    EventType,
    FlowCompletedEvent,
    FlowFailedEvent,
    FlowSkippedEvent,
    FlowStartedEvent,
    RunCompletedEvent,
    RunFailedEvent,
    RunStartedEvent,
)

__all__ = [
    "_ReconstructedEvent",
    "_reconstruct_lifecycle_events",
]

_LIFECYCLE_KINDS = {SpanKind.DEPLOYMENT, SpanKind.FLOW, SpanKind.TASK}

_KIND_PRIORITY = {SpanKind.DEPLOYMENT: 0, SpanKind.FLOW: 1, SpanKind.TASK: 2}
_PHASE_PRIORITY_STARTED = 0
_PHASE_PRIORITY_SKIPPED = 1
_PHASE_PRIORITY_TERMINAL = 2


@dataclass(frozen=True, slots=True)
class _ReconstructedEvent:
    """A lifecycle event reconstructed from database spans."""

    event_type: EventType
    span_id: str
    timestamp: datetime
    data: dict[str, Any]


def _parse_meta(span: SpanRecord) -> dict[str, Any]:
    return parse_json_object(span.meta_json, context=f"Span {span.span_id}", field_name="meta_json")


def _parse_receiver(span: SpanRecord) -> dict[str, Any]:
    if not span.receiver_json:
        return {}
    parsed = parse_json_object(span.receiver_json, context=f"Span {span.span_id}", field_name="receiver_json")
    return parsed.get("value", {}) if isinstance(parsed.get("value"), dict) else {}


def _parse_output(span: SpanRecord) -> dict[str, Any]:
    if not span.output_json:
        return {}
    try:
        value = json.loads(span.output_json)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _duration_ms(span: SpanRecord) -> int:
    if span.started_at and span.ended_at:
        return int((span.ended_at - span.started_at).total_seconds() * 1000)
    return 0


def _class_name_from_target(target: str) -> str:
    if not target:
        return ""
    if ":" in target:
        qual_part = target.rsplit(":", 1)[-1]
        parts = qual_part.split(".")
        return parts[0] if len(parts) >= 2 else qual_part
    return target.rsplit(".", 1)[-1]


def _build_doc_refs(shas: tuple[str, ...], doc_map: dict[str, DocumentRecord]) -> tuple[DocumentRef, ...]:
    refs: list[DocumentRef] = []
    for sha in shas:
        record = doc_map.get(sha)
        if record is not None:
            refs.append(DocumentRef.from_record(record))
    return tuple(refs)


def _sort_key(event: _ReconstructedEvent) -> tuple[datetime, int, int, str]:
    kind_priority = 1
    phase_priority = _PHASE_PRIORITY_STARTED
    event_type = event.event_type

    if event_type in {EventType.RUN_STARTED, EventType.RUN_COMPLETED, EventType.RUN_FAILED}:
        kind_priority = _KIND_PRIORITY[SpanKind.DEPLOYMENT]
    elif event_type in {EventType.FLOW_STARTED, EventType.FLOW_COMPLETED, EventType.FLOW_FAILED, EventType.FLOW_SKIPPED}:
        kind_priority = _KIND_PRIORITY[SpanKind.FLOW]
    elif event_type in {EventType.TASK_STARTED, EventType.TASK_COMPLETED, EventType.TASK_FAILED}:
        kind_priority = _KIND_PRIORITY[SpanKind.TASK]

    if event_type in {EventType.FLOW_SKIPPED}:
        phase_priority = _PHASE_PRIORITY_SKIPPED
    elif event_type in {
        EventType.RUN_COMPLETED,
        EventType.RUN_FAILED,
        EventType.FLOW_COMPLETED,
        EventType.FLOW_FAILED,
        EventType.TASK_COMPLETED,
        EventType.TASK_FAILED,
    }:
        phase_priority = _PHASE_PRIORITY_TERMINAL

    return (event.timestamp, kind_priority, phase_priority, event.span_id)


def _reconstruct_deployment_events(
    span: SpanRecord,
    meta: dict[str, Any],
    parent_task_id_str: str | None,
) -> list[_ReconstructedEvent]:
    events: list[_ReconstructedEvent] = []
    span_id_str = str(span.span_id)
    root_id_str = str(span.root_deployment_id)

    started_event = RunStartedEvent(
        run_id=span.run_id,
        span_id=span_id_str,
        root_deployment_id=root_id_str,
        parent_deployment_task_id=parent_task_id_str,
        input_fingerprint=meta.get("input_fingerprint", ""),
        status=str(SpanStatus.RUNNING),
        deployment_name=span.deployment_name or span.name,
        deployment_class=meta.get("deployment_class", ""),
        flow_plan=meta.get("flow_plan", []),
        parent_span_id=str(span.parent_span_id) if span.parent_span_id else "",
        input_document_sha256s=span.input_document_shas,
    )
    events.append(
        _ReconstructedEvent(
            event_type=EventType.RUN_STARTED,
            span_id=span_id_str,
            timestamp=span.started_at,
            data=event_to_payload(started_event),
        )
    )

    if span.status == SpanStatus.COMPLETED:
        output = _parse_output(span)
        events.append(
            _ReconstructedEvent(
                event_type=EventType.RUN_COMPLETED,
                span_id=span_id_str,
                timestamp=span.ended_at or span.started_at,
                data=event_to_payload(
                    RunCompletedEvent(
                        run_id=span.run_id,
                        span_id=span_id_str,
                        root_deployment_id=root_id_str,
                        parent_deployment_task_id=parent_task_id_str,
                        status=str(span.status),
                        result=output.get("result", {}),
                        deployment_name=span.deployment_name or span.name,
                        deployment_class=meta.get("deployment_class", ""),
                        duration_ms=_duration_ms(span),
                        output_document_sha256s=span.output_document_shas,
                        parent_span_id=str(span.parent_span_id) if span.parent_span_id else "",
                    )
                ),
            )
        )
    elif span.status == SpanStatus.FAILED:
        error_code_str = meta.get("error_code", "unknown")
        try:
            error_code = ErrorCode(error_code_str)
        except ValueError:
            error_code = ErrorCode.UNKNOWN
        events.append(
            _ReconstructedEvent(
                event_type=EventType.RUN_FAILED,
                span_id=span_id_str,
                timestamp=span.ended_at or span.started_at,
                data=event_to_payload(
                    RunFailedEvent(
                        run_id=span.run_id,
                        span_id=span_id_str,
                        root_deployment_id=root_id_str,
                        parent_deployment_task_id=parent_task_id_str,
                        status=str(span.status),
                        error_code=error_code,
                        error_message=span.error_message,
                        deployment_name=span.deployment_name or span.name,
                        deployment_class=meta.get("deployment_class", ""),
                        duration_ms=_duration_ms(span),
                        parent_span_id=str(span.parent_span_id) if span.parent_span_id else "",
                    )
                ),
            )
        )

    return events


def _reconstruct_flow_events(
    span: SpanRecord,
    meta: dict[str, Any],
    parent_task_id_str: str | None,
    deployment_span_id: str,
    doc_map: dict[str, DocumentRecord],
) -> list[_ReconstructedEvent]:
    events: list[_ReconstructedEvent] = []
    span_id_str = str(span.span_id)
    root_id_str = str(span.root_deployment_id)
    flow_class = meta.get("flow_class") or _class_name_from_target(span.target)
    step = meta.get("step", 0)
    total_steps = meta.get("total_steps", 0)

    if span.status in {SpanStatus.SKIPPED, SpanStatus.CACHED}:
        events.append(
            _ReconstructedEvent(
                event_type=EventType.FLOW_SKIPPED,
                span_id=span_id_str,
                timestamp=span.started_at,
                data=event_to_payload(
                    FlowSkippedEvent(
                        run_id=span.run_id,
                        span_id=span_id_str,
                        root_deployment_id=root_id_str,
                        parent_deployment_task_id=parent_task_id_str,
                        flow_name=span.name,
                        flow_class=flow_class,
                        step=step,
                        total_steps=total_steps,
                        status=str(span.status),
                        reason=meta.get("skip_reason", ""),
                        parent_span_id=deployment_span_id,
                        input_document_sha256s=span.input_document_shas,
                    )
                ),
            )
        )
        return events

    flow_params = _parse_receiver(span)
    started_event = FlowStartedEvent(
        run_id=span.run_id,
        span_id=span_id_str,
        root_deployment_id=root_id_str,
        parent_deployment_task_id=parent_task_id_str,
        flow_name=span.name,
        flow_class=flow_class,
        step=step,
        total_steps=total_steps,
        status=str(SpanStatus.RUNNING),
        expected_tasks=meta.get("expected_tasks", []),
        flow_params=flow_params,
        parent_span_id=deployment_span_id,
        input_document_sha256s=span.input_document_shas,
    )
    events.append(
        _ReconstructedEvent(
            event_type=EventType.FLOW_STARTED,
            span_id=span_id_str,
            timestamp=span.started_at,
            data=event_to_payload(started_event),
        )
    )

    if span.status == SpanStatus.COMPLETED:
        events.append(
            _ReconstructedEvent(
                event_type=EventType.FLOW_COMPLETED,
                span_id=span_id_str,
                timestamp=span.ended_at or span.started_at,
                data=event_to_payload(
                    FlowCompletedEvent(
                        run_id=span.run_id,
                        span_id=span_id_str,
                        root_deployment_id=root_id_str,
                        parent_deployment_task_id=parent_task_id_str,
                        flow_name=span.name,
                        flow_class=flow_class,
                        step=step,
                        total_steps=total_steps,
                        status=str(span.status),
                        duration_ms=_duration_ms(span),
                        output_documents=_build_doc_refs(span.output_document_shas, doc_map),
                        parent_span_id=deployment_span_id,
                        input_document_sha256s=span.input_document_shas,
                    )
                ),
            )
        )
    elif span.status == SpanStatus.FAILED:
        events.append(
            _ReconstructedEvent(
                event_type=EventType.FLOW_FAILED,
                span_id=span_id_str,
                timestamp=span.ended_at or span.started_at,
                data=event_to_payload(
                    FlowFailedEvent(
                        run_id=span.run_id,
                        span_id=span_id_str,
                        root_deployment_id=root_id_str,
                        parent_deployment_task_id=parent_task_id_str,
                        flow_name=span.name,
                        flow_class=flow_class,
                        step=step,
                        total_steps=total_steps,
                        status=str(span.status),
                        error_message=span.error_message,
                        parent_span_id=deployment_span_id,
                        input_document_sha256s=span.input_document_shas,
                    )
                ),
            )
        )

    return events


def _reconstruct_task_events(
    span: SpanRecord,
    meta: dict[str, Any],
    parent_task_id_str: str | None,
    *,
    flow_span: SpanRecord | None,
    flow_meta: dict[str, Any],
    doc_map: dict[str, DocumentRecord],
) -> list[_ReconstructedEvent]:
    events: list[_ReconstructedEvent] = []
    span_id_str = str(span.span_id)
    root_id_str = str(span.root_deployment_id)
    task_class = meta.get("task_class") or _class_name_from_target(span.target)
    flow_name = flow_span.name if flow_span else ""
    step = flow_meta.get("step", 0) if flow_meta else 0
    total_steps = flow_meta.get("total_steps", 0) if flow_meta else 0
    # Point to the logical FLOW span, not an ATTEMPT span (R2: task events reference flow hierarchy)
    parent_span_id = str(flow_span.span_id) if flow_span else (str(span.parent_span_id) if span.parent_span_id else "")

    is_cached = span.status == SpanStatus.CACHED

    if not is_cached:
        events.append(
            _ReconstructedEvent(
                event_type=EventType.TASK_STARTED,
                span_id=span_id_str,
                timestamp=span.started_at,
                data=event_to_payload(
                    TaskStartedEvent(
                        run_id=span.run_id,
                        span_id=span_id_str,
                        root_deployment_id=root_id_str,
                        parent_deployment_task_id=parent_task_id_str,
                        flow_name=flow_name,
                        step=step,
                        total_steps=total_steps,
                        status=str(SpanStatus.RUNNING),
                        task_name=span.name,
                        task_class=task_class,
                        parent_span_id=parent_span_id,
                        input_document_sha256s=span.input_document_shas,
                    )
                ),
            )
        )

    if span.status in {SpanStatus.COMPLETED, SpanStatus.CACHED}:
        events.append(
            _ReconstructedEvent(
                event_type=EventType.TASK_COMPLETED,
                span_id=span_id_str,
                timestamp=span.ended_at or span.started_at,
                data=event_to_payload(
                    TaskCompletedEvent(
                        run_id=span.run_id,
                        span_id=span_id_str,
                        root_deployment_id=root_id_str,
                        parent_deployment_task_id=parent_task_id_str,
                        flow_name=flow_name,
                        step=step,
                        total_steps=total_steps,
                        status="cached" if is_cached else str(span.status),
                        task_name=span.name,
                        task_class=task_class,
                        duration_ms=_duration_ms(span),
                        output_documents=_build_doc_refs(span.output_document_shas, doc_map),
                        parent_span_id=parent_span_id,
                        input_document_sha256s=span.input_document_shas,
                    )
                ),
            )
        )
    elif span.status == SpanStatus.FAILED:
        events.append(
            _ReconstructedEvent(
                event_type=EventType.TASK_FAILED,
                span_id=span_id_str,
                timestamp=span.ended_at or span.started_at,
                data=event_to_payload(
                    TaskFailedEvent(
                        run_id=span.run_id,
                        span_id=span_id_str,
                        root_deployment_id=root_id_str,
                        parent_deployment_task_id=parent_task_id_str,
                        flow_name=flow_name,
                        step=step,
                        total_steps=total_steps,
                        status=str(span.status),
                        task_name=span.name,
                        task_class=task_class,
                        error_message=span.error_message,
                        parent_span_id=parent_span_id,
                        input_document_sha256s=span.input_document_shas,
                    )
                ),
            )
        )

    return events


async def _reconstruct_lifecycle_events(
    reader: DatabaseReader,
    root_deployment_id: UUID,
) -> list[_ReconstructedEvent]:
    """Reconstruct lifecycle events from a deployment's span tree.

    Returns events in chronological order. Each event's data dict matches
    the payload shape published by PubSubPublisher (via event_to_payload()).

    Heartbeats are not reconstructed (ephemeral, not stored in spans).
    """
    all_spans = await reader.get_deployment_tree(root_deployment_id)

    lifecycle_spans = [s for s in all_spans if s.kind in _LIFECYCLE_KINDS]
    if not lifecycle_spans:
        return []

    all_doc_shas: set[str] = set()
    for span in lifecycle_spans:
        all_doc_shas.update(span.input_document_shas)
        all_doc_shas.update(span.output_document_shas)

    doc_map: dict[str, DocumentRecord] = {}
    if all_doc_shas:
        doc_map = await reader.get_documents_batch(sorted(all_doc_shas))

    # Lookup for ALL spans — needed to walk through ATTEMPT parents to find FLOW context
    all_span_by_id: dict[UUID, SpanRecord] = {s.span_id: s for s in all_spans}
    meta_by_id: dict[UUID, dict[str, Any]] = {s.span_id: _parse_meta(s) for s in lifecycle_spans}

    deployment_by_id: dict[UUID, SpanRecord] = {span.span_id: span for span in lifecycle_spans if span.kind == SpanKind.DEPLOYMENT}

    events: list[_ReconstructedEvent] = []

    for span in lifecycle_spans:
        meta = meta_by_id[span.span_id]
        deployment_span = deployment_by_id.get(span.deployment_id)
        parent_task_id_str = str(deployment_span.parent_span_id) if deployment_span and deployment_span.parent_span_id else None
        deployment_span_id_str = str(deployment_span.span_id) if deployment_span is not None else ""

        if span.kind == SpanKind.DEPLOYMENT:
            events.extend(_reconstruct_deployment_events(span, meta, parent_task_id_str))

        elif span.kind == SpanKind.FLOW:
            events.extend(_reconstruct_flow_events(span, meta, parent_task_id_str, deployment_span_id_str, doc_map))

        elif span.kind == SpanKind.TASK:
            # Walk up through ATTEMPT spans to find the enclosing FLOW span
            parent = all_span_by_id.get(span.parent_span_id) if span.parent_span_id else None
            while parent is not None and parent.kind == SpanKind.ATTEMPT:
                parent = all_span_by_id.get(parent.parent_span_id) if parent.parent_span_id else None
            flow_span = parent if (parent is not None and parent.kind == SpanKind.FLOW) else None
            flow_meta = meta_by_id.get(flow_span.span_id, {}) if flow_span else {}
            events.extend(_reconstruct_task_events(span, meta, parent_task_id_str, flow_span=flow_span, flow_meta=flow_meta, doc_map=doc_map))

    events.sort(key=_sort_key)
    return events
