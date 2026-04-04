"""Document provenance computation for trace-inspector."""

from collections import Counter, defaultdict
from uuid import UUID

from ai_pipeline_core.documents.utils import _is_document_sha256
from trace_inspector._types import LoadedTask, LoadedTrace, ProvenanceEntry

__all__ = [
    "build_provenance",
]


def build_provenance(trace: LoadedTrace) -> dict[str, ProvenanceEntry]:
    """Build producer and consumer maps for all loaded documents."""
    flow_labels = _flow_label_map(trace)
    producer_task_ids: dict[str, UUID | None] = {document_sha: None for document_sha in trace.documents}
    consumer_task_ids: dict[str, list[UUID]] = {document_sha: [] for document_sha in trace.documents}

    ordered_tasks = sorted(trace.tasks.values(), key=lambda task: (task.parent_flow_span.sequence_no, task.span.sequence_no, task.span.started_at))
    for task in ordered_tasks:
        new_outputs = set(task.output_document_shas) - set(task.input_document_shas)
        for document_sha in new_outputs:
            if document_sha in producer_task_ids and producer_task_ids[document_sha] is None:
                producer_task_ids[document_sha] = task.span.span_id
        for document_sha in task.input_document_shas:
            if document_sha in consumer_task_ids:
                consumer_task_ids[document_sha].append(task.span.span_id)

    provenance: dict[str, ProvenanceEntry] = {}
    for document_sha, document in trace.documents.items():
        produced_by_task = _resolve_task(trace, producer_task_ids.get(document_sha))
        produced_by_label = "root input"
        if produced_by_task is not None:
            produced_by_label = _task_label(produced_by_task, flow_labels)
        external_source_labels = tuple(sorted(source for source in document.record.derived_from if not _is_document_sha256(source)))
        if produced_by_task is None and external_source_labels:
            produced_by_label = f"external: {external_source_labels[0]}"
        consumed_task_spans = tuple(task for task_id in consumer_task_ids.get(document_sha, []) if (task := _resolve_task(trace, task_id)) is not None)
        provenance[document_sha] = ProvenanceEntry(
            document_sha256=document_sha,
            produced_by_task_id=None if produced_by_task is None else produced_by_task.span.span_id,
            produced_by_label=produced_by_label,
            consumed_by_task_ids=tuple(task.span.span_id for task in consumed_task_spans),
            consumed_by_labels=tuple(_task_label(task, flow_labels) for task in consumed_task_spans),
            derived_from_labels=tuple(_short_document_reference(trace, value) for value in document.record.derived_from if _is_document_sha256(value)),
            triggered_by_labels=tuple(_short_document_reference(trace, value) for value in document.record.triggered_by),
            external_source_labels=external_source_labels,
        )
    return provenance


def _resolve_task(trace: LoadedTrace, task_id: UUID | None) -> LoadedTask | None:
    if task_id is None:
        return None
    return trace.tasks.get(task_id)


def _task_label(task: LoadedTask, flow_labels: dict[UUID, str]) -> str:
    return f"{task.span.name} in {flow_labels[task.parent_flow_span.span_id]}"


def _flow_label_map(trace: LoadedTrace) -> dict[UUID, str]:
    name_counts = Counter(flow.name for flow in trace.flow_spans)
    seen_counts: defaultdict[str, int] = defaultdict(int)
    labels: dict[UUID, str] = {}
    for flow in trace.flow_spans:
        seen_counts[flow.name] += 1
        label = flow.name
        if name_counts[flow.name] > 1:
            label = f"{flow.name} (seq {seen_counts[flow.name]})"
        labels[flow.span_id] = label
    return labels


def _short_document_reference(trace: LoadedTrace, document_sha: str) -> str:
    document = trace.documents.get(document_sha)
    if document is None:
        return document_sha
    return document.output_filename
