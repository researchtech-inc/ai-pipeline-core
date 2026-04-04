"""Trace loading and execution-graph assembly."""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any
from uuid import UUID

from ai_pipeline_core.database import DocumentRecord, SpanKind, SpanRecord
from ai_pipeline_core.database._protocol import DatabaseReader
from ai_pipeline_core.database._types import get_token_count
from trace_inspector._helpers import duration_seconds, parse_json_object_lenient
from trace_inspector._types import (
    LoadedAttachment,
    LoadedDocument,
    LoadedTask,
    LoadedTrace,
    document_filename,
    is_textual_mime_type,
    make_short_id_map,
)

__all__ = [
    "load_trace",
]

logger = logging.getLogger(__name__)

TOKENS_INPUT_KEY = "tokens_input"
TOKENS_OUTPUT_KEY = "tokens_output"
TOKENS_CACHE_READ_KEY = "tokens_cache_read"
TOKENS_REASONING_KEY = "tokens_reasoning"


async def load_trace(reader: DatabaseReader, deployment_id: UUID, *, reader_label: str = "") -> LoadedTrace:
    """Load the full trace graph for one deployment tree."""
    spans = await reader.get_deployment_tree(deployment_id)
    if not spans:
        msg = f"Deployment {deployment_id} returned no spans. Pass a valid deployment id from ai-trace list or ai-trace show."
        raise ValueError(msg)

    spans_by_id = {span.span_id: span for span in spans}
    children_map = _build_children_map(spans, spans_by_id)
    root_span = _resolve_root_span(spans, deployment_id)
    flow_spans = tuple(sorted((span for span in spans if span.kind == SpanKind.FLOW), key=_span_sort_key))
    documents = await _load_documents(reader, deployment_id)
    tasks_by_flow, parent_task_by_task, child_tasks_by_task = _group_task_ids(spans, spans_by_id)
    descendant_metrics = _compute_descendant_metrics(spans_by_id, children_map)
    loaded_tasks = _build_loaded_tasks(
        flow_spans=flow_spans,
        spans_by_id=spans_by_id,
        children_map=children_map,
        tasks_by_flow=tasks_by_flow,
        parent_task_by_task=parent_task_by_task,
        child_tasks_by_task=child_tasks_by_task,
        descendant_metrics=descendant_metrics,
    )

    frozen_children_map = {parent_id: tuple(child_ids) for parent_id, child_ids in children_map.items()}
    frozen_tasks_by_flow = {flow_id: tuple(task_ids) for flow_id, task_ids in tasks_by_flow.items()}
    return LoadedTrace(
        root_span=root_span,
        spans_by_id=spans_by_id,
        children_map=frozen_children_map,
        flow_spans=flow_spans,
        tasks_by_flow=frozen_tasks_by_flow,
        tasks=loaded_tasks,
        documents=documents,
        reader_label=reader_label,
    )


def _build_children_map(spans: list[SpanRecord], spans_by_id: dict[UUID, SpanRecord]) -> dict[UUID | None, list[UUID]]:
    children_map: dict[UUID | None, list[UUID]] = defaultdict(list)
    for span in spans:
        children_map[span.parent_span_id].append(span.span_id)
    for parent_id, child_ids in children_map.items():
        children_map[parent_id] = sorted(child_ids, key=lambda child_id: _span_sort_key(spans_by_id[child_id]))
    return dict(children_map)


def _resolve_root_span(spans: list[SpanRecord], deployment_id: UUID) -> SpanRecord:
    for span in spans:
        if span.kind == SpanKind.DEPLOYMENT and span.root_deployment_id == deployment_id and span.parent_span_id is None:
            return span
    for span in spans:
        if span.kind == SpanKind.DEPLOYMENT and span.root_deployment_id == deployment_id:
            return span
    return min(spans, key=_span_sort_key)


async def _load_documents(reader: DatabaseReader, deployment_id: UUID) -> dict[str, LoadedDocument]:
    document_shas = sorted(await reader.get_all_document_shas_for_tree(deployment_id))
    document_records = await reader.get_documents_batch(document_shas)
    blob_shas = _collect_blob_shas(document_records)
    blob_records = await reader.get_blobs_batch(sorted(blob_shas))
    short_ids = make_short_id_map(document_shas)
    return _build_loaded_documents(document_records, blob_records, short_ids)


def _group_task_ids(
    spans: list[SpanRecord],
    spans_by_id: dict[UUID, SpanRecord],
) -> tuple[dict[UUID, list[UUID]], dict[UUID, UUID | None], dict[UUID, list[UUID]]]:
    parent_task_by_task: dict[UUID, UUID | None] = {}
    child_tasks_by_task: dict[UUID, list[UUID]] = defaultdict(list)
    tasks_by_flow: dict[UUID, list[UUID]] = defaultdict(list)
    for span in spans:
        if span.kind != SpanKind.TASK:
            continue
        flow_id = _find_nearest_ancestor_of_kind(span, spans_by_id, SpanKind.FLOW)
        if flow_id is None:
            logger.warning(
                "Task span %s (%s) has no ancestor flow span and will be excluded from the trace bundle. "
                "Check whether the deployment recorded a flow span for this task.",
                span.span_id,
                span.name,
            )
            continue
        tasks_by_flow[flow_id].append(span.span_id)
        parent_task_id = _find_nearest_ancestor_of_kind(span, spans_by_id, SpanKind.TASK)
        parent_task_by_task[span.span_id] = parent_task_id
        if parent_task_id is not None:
            child_tasks_by_task[parent_task_id].append(span.span_id)
    return tasks_by_flow, parent_task_by_task, child_tasks_by_task


def _build_loaded_tasks(
    *,
    flow_spans: tuple[SpanRecord, ...],
    spans_by_id: dict[UUID, SpanRecord],
    children_map: dict[UUID | None, list[UUID]],
    tasks_by_flow: dict[UUID, list[UUID]],
    parent_task_by_task: dict[UUID, UUID | None],
    child_tasks_by_task: dict[UUID, list[UUID]],
    descendant_metrics: dict[UUID, dict[str, float | int]],
) -> dict[UUID, LoadedTask]:
    loaded_tasks: dict[UUID, LoadedTask] = {}
    for flow_span in flow_spans:
        task_ids = sorted(tasks_by_flow.get(flow_span.span_id, []), key=lambda task_id: _span_sort_key(spans_by_id[task_id]))
        tasks_by_flow[flow_span.span_id] = task_ids
        for task_id in task_ids:
            loaded_tasks[task_id] = _build_loaded_task(
                task_id=task_id,
                flow_span=flow_span,
                spans_by_id=spans_by_id,
                children_map=children_map,
                parent_task_by_task=parent_task_by_task,
                child_tasks_by_task=child_tasks_by_task,
                descendant_metrics=descendant_metrics,
            )
    return loaded_tasks


def _build_loaded_task(
    *,
    task_id: UUID,
    flow_span: SpanRecord,
    spans_by_id: dict[UUID, SpanRecord],
    children_map: dict[UUID | None, list[UUID]],
    parent_task_by_task: dict[UUID, UUID | None],
    child_tasks_by_task: dict[UUID, list[UUID]],
    descendant_metrics: dict[UUID, dict[str, float | int]],
) -> LoadedTask:
    task_span = spans_by_id[task_id]
    attempt_spans = tuple(_child_spans_of_kind(task_span.span_id, spans_by_id, children_map, SpanKind.ATTEMPT))
    conversation_spans, llm_rounds_by_conversation, tool_calls_by_conversation = _collect_task_conversations(task_id, spans_by_id, children_map)
    metrics = descendant_metrics.get(task_id, {})
    return LoadedTask(
        span=task_span,
        parent_flow_span=flow_span,
        attempt_spans=attempt_spans,
        conversation_spans=conversation_spans,
        llm_rounds_by_conversation=llm_rounds_by_conversation,
        tool_calls_by_conversation=tool_calls_by_conversation,
        input_document_shas=task_span.input_document_shas,
        output_document_shas=task_span.output_document_shas,
        descendant_cost_usd=float(metrics.get("cost_usd", 0.0)),
        duration_seconds=duration_seconds(task_span),
        tokens_input=int(metrics.get("tokens_input", 0)),
        tokens_output=int(metrics.get("tokens_output", 0)),
        tokens_cache_read=int(metrics.get("tokens_cache_read", 0)),
        tokens_reasoning=int(metrics.get("tokens_reasoning", 0)),
        parent_task_id=parent_task_by_task.get(task_id),
        child_task_ids=tuple(sorted(child_tasks_by_task.get(task_id, []), key=lambda child_id: _span_sort_key(spans_by_id[child_id]))),
    )


def _collect_blob_shas(document_records: dict[str, DocumentRecord]) -> set[str]:
    blob_shas: set[str] = set()
    for record in document_records.values():
        blob_shas.add(record.content_sha256)
        blob_shas.update(record.attachment_content_sha256s)
    return blob_shas


def _build_loaded_documents(
    document_records: dict[str, DocumentRecord],
    blob_records: dict[str, Any],
    short_ids: dict[str, str],
) -> dict[str, LoadedDocument]:
    documents: dict[str, LoadedDocument] = {}
    for sha, record in document_records.items():
        blob = blob_records.get(record.content_sha256)
        content = None if blob is None else blob.content
        if blob is None:
            logger.warning("Blob %s for document %s (%s) not found — content will be unavailable.", record.content_sha256[:12], sha[:12], record.name)
        text_content = _decode_text_content(content, record.mime_type)
        attachments: list[LoadedAttachment] = []
        for name, mime_type, size_bytes, attachment_sha in zip(
            record.attachment_names,
            record.attachment_mime_types,
            record.attachment_size_bytes,
            record.attachment_content_sha256s,
            strict=True,
        ):
            attachment_blob = blob_records.get(attachment_sha)
            attachment_content = None if attachment_blob is None else attachment_blob.content
            attachments.append(
                LoadedAttachment(
                    name=name,
                    mime_type=mime_type,
                    size_bytes=size_bytes,
                    content_sha256=attachment_sha,
                    content=attachment_content,
                    text_content=_decode_text_content(attachment_content, mime_type),
                )
            )
        short_id = short_ids[sha]
        documents[sha] = LoadedDocument(
            record=record,
            short_id=short_id,
            output_filename=document_filename(short_id),
            content=content,
            text_content=text_content,
            attachments=tuple(attachments),
        )
    return documents


def _decode_text_content(content: bytes | None, mime_type: str) -> str | None:
    if content is None:
        return None
    if not is_textual_mime_type(mime_type):
        return None
    return content.decode("utf-8", errors="replace")


def _find_nearest_ancestor_of_kind(span: SpanRecord, spans_by_id: dict[UUID, SpanRecord], kind: str) -> UUID | None:
    parent_id = span.parent_span_id
    while parent_id is not None:
        parent_span = spans_by_id[parent_id]
        if parent_span.kind == kind:
            return parent_id
        parent_id = parent_span.parent_span_id
    return None


def _child_spans_of_kind(
    parent_id: UUID,
    spans_by_id: dict[UUID, SpanRecord],
    children_map: dict[UUID | None, list[UUID]],
    kind: str,
) -> list[SpanRecord]:
    return [spans_by_id[child_id] for child_id in children_map.get(parent_id, []) if spans_by_id[child_id].kind == kind]


def _collect_descendant_spans_of_kind(
    root_id: UUID,
    spans_by_id: dict[UUID, SpanRecord],
    children_map: dict[UUID | None, list[UUID]],
    kind: str,
) -> list[SpanRecord]:
    """Recursively collect all descendant spans of a given kind under root_id."""
    result: list[SpanRecord] = []
    stack = list(children_map.get(root_id, []))
    while stack:
        child_id = stack.pop()
        child = spans_by_id[child_id]
        if child.kind == kind:
            result.append(child)
        stack.extend(children_map.get(child_id, []))
    result.sort(key=_span_sort_key)
    return result


def _collect_task_conversations(
    task_id: UUID,
    spans_by_id: dict[UUID, SpanRecord],
    children_map: dict[UUID | None, list[UUID]],
) -> tuple[tuple[SpanRecord, ...], dict[UUID, tuple[SpanRecord, ...]], dict[UUID, tuple[SpanRecord, ...]]]:
    """Collect conversations from all descendants of a task (not just direct attempt children).

    Walks the full subtree under each attempt span to find CONVERSATION spans nested
    under OPERATION or other intermediate span kinds.
    """
    llm_rounds_by_conversation: dict[UUID, tuple[SpanRecord, ...]] = {}
    tool_calls_by_conversation: dict[UUID, tuple[SpanRecord, ...]] = {}
    attempt_ids = [child_id for child_id in children_map.get(task_id, []) if spans_by_id[child_id].kind == SpanKind.ATTEMPT]
    conversations: list[SpanRecord] = []
    for attempt_id in attempt_ids:
        attempt_conversations = _collect_descendant_spans_of_kind(attempt_id, spans_by_id, children_map, SpanKind.CONVERSATION)
        for conversation_span in attempt_conversations:
            conversations.append(conversation_span)
            llm_rounds = _collect_descendant_spans_of_kind(conversation_span.span_id, spans_by_id, children_map, SpanKind.LLM_ROUND)
            tool_calls = _collect_descendant_spans_of_kind(conversation_span.span_id, spans_by_id, children_map, SpanKind.TOOL_CALL)
            llm_rounds_by_conversation[conversation_span.span_id] = tuple(llm_rounds)
            tool_calls_by_conversation[conversation_span.span_id] = tuple(tool_calls)
    return tuple(conversations), llm_rounds_by_conversation, tool_calls_by_conversation


def _span_sort_key(span: SpanRecord) -> tuple[int, str, str]:
    return span.sequence_no, span.started_at.isoformat(), str(span.span_id)


def _compute_descendant_metrics(
    spans_by_id: dict[UUID, SpanRecord],
    children_map: dict[UUID | None, list[UUID]],
) -> dict[UUID, dict[str, float | int]]:
    metrics_by_span_id: dict[UUID, dict[str, float | int]] = {}
    visiting: set[UUID] = set()
    for span_id in spans_by_id:
        _descendant_metrics(span_id, spans_by_id, children_map, metrics_by_span_id, visiting)
    return metrics_by_span_id


def _descendant_metrics(
    span_id: UUID,
    spans_by_id: dict[UUID, SpanRecord],
    children_map: dict[UUID | None, list[UUID]],
    metrics_by_span_id: dict[UUID, dict[str, float | int]],
    visiting: set[UUID],
) -> dict[str, float | int]:
    if span_id in metrics_by_span_id:
        return metrics_by_span_id[span_id]
    if span_id in visiting:
        return {"cost_usd": 0.0, "tokens_input": 0, "tokens_output": 0, "tokens_cache_read": 0, "tokens_reasoning": 0}

    visiting.add(span_id)
    metrics: dict[str, float | int] = {"cost_usd": 0.0, "tokens_input": 0, "tokens_output": 0, "tokens_cache_read": 0, "tokens_reasoning": 0}
    for child_id in children_map.get(span_id, []):
        child = spans_by_id[child_id]
        metrics["cost_usd"] += child.cost_usd
        if child.kind == SpanKind.LLM_ROUND:
            child_metrics = parse_json_object_lenient(child.metrics_json, context=f"Span {child_id}")
            metrics["tokens_input"] += get_token_count(child_metrics, TOKENS_INPUT_KEY)
            metrics["tokens_output"] += get_token_count(child_metrics, TOKENS_OUTPUT_KEY)
            metrics["tokens_cache_read"] += get_token_count(child_metrics, TOKENS_CACHE_READ_KEY)
            metrics["tokens_reasoning"] += get_token_count(child_metrics, TOKENS_REASONING_KEY)
        descendant = _descendant_metrics(child_id, spans_by_id, children_map, metrics_by_span_id, visiting)
        metrics["cost_usd"] += float(descendant["cost_usd"])
        metrics["tokens_input"] += int(descendant["tokens_input"])
        metrics["tokens_output"] += int(descendant["tokens_output"])
        metrics["tokens_cache_read"] += int(descendant["tokens_cache_read"])
        metrics["tokens_reasoning"] += int(descendant["tokens_reasoning"])
    metrics_by_span_id[span_id] = metrics
    visiting.discard(span_id)
    return metrics
