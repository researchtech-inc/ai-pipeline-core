"""Markdown rendering for trace-inspector bundles."""

from __future__ import annotations

from uuid import UUID

from trace_inspector._helpers import duration_seconds
from trace_inspector._render_helpers import (
    conversation_metadata_line,
    display_flow_name,
    doc_link,
    fence_language,
    flow_cost,
    format_duration_seconds,
    format_label_summary,
    is_batchable_task,
    preview_or_truncate_attachment,
    relative_link,
    render_task_document_block,
    root_task_ids,
    summarize_totals,
    task_row_cost,
    task_row_duration_seconds,
    task_row_label,
    task_row_status,
    task_row_token_summary,
    task_token_summary,
)
from trace_inspector._truncation import binary_notice, should_inline_document
from trace_inspector._types import LoadedDocument, LoadedTask, LoadedTrace, ProvenanceEntry, RenderConfig
from trace_inspector._xml import render_message_sequence

__all__ = [
    "build_batch_groups",
    "render_batch_markdown",
    "render_comparison_markdown",
    "render_document_file",
    "render_flow_markdown",
    "render_index_markdown",
    "render_task_markdown",
]

MAX_INDEX_TASK_ROWS = 100

type TaskRenderGroup = tuple[str, tuple[UUID, ...]]
type FlowTaskRow = tuple[int, int, tuple[UUID, ...]]


def build_batch_groups(
    trace: LoadedTrace,
    ordered_task_ids: tuple[UUID, ...],
    *,
    selected_task_ids: set[UUID],
    batch_threshold: int,
) -> list[TaskRenderGroup]:
    """Build task render groups for one sibling sequence, collapsing consecutive identical leaf tasks."""
    groups: list[TaskRenderGroup] = []
    index = 0
    while index < len(ordered_task_ids):
        current_task_id = ordered_task_ids[index]
        current_name = trace.tasks[current_task_id].span.name
        current_is_batchable = is_batchable_task(trace, current_task_id, selected_task_ids)
        run_ids = [current_task_id]
        cursor = index + 1
        while cursor < len(ordered_task_ids):
            candidate_task_id = ordered_task_ids[cursor]
            if trace.tasks[candidate_task_id].span.name != current_name:
                break
            if is_batchable_task(trace, candidate_task_id, selected_task_ids) != current_is_batchable:
                break
            run_ids.append(ordered_task_ids[cursor])
            cursor += 1
        if current_is_batchable and len(run_ids) >= batch_threshold:
            groups.append(("batch", tuple(run_ids)))
        else:
            groups.extend(("task", (task_id,)) for task_id in run_ids)
        index = cursor
    return groups


def render_index_markdown(
    trace: LoadedTrace,
    *,
    selected_flow_ids: set[UUID],
    selected_task_ids: set[UUID],
    flow_file_map: dict[UUID, str],
    task_file_map: dict[UUID, str],
    batch_threshold: int,
) -> str:
    """Render the top-level index markdown file."""
    totals = summarize_totals(trace)
    collapsed_task_row_count = sum(
        len(_flow_task_rows(trace, flow_span.span_id, selected_task_ids, batch_threshold))
        for flow_span in trace.flow_spans
        if flow_span.span_id in selected_flow_ids
    )
    failed_tasks = [task for task in trace.tasks.values() if task.span.status == "failed" and task.span.span_id in selected_task_ids]
    lines = [
        f"# Run: {trace.root_span.deployment_name or trace.root_span.name}",
        "",
        f"- Run ID: `{trace.root_span.run_id}`",
        f"- Deployment ID: `{trace.root_span.root_deployment_id}`",
        f"- Status: `{trace.root_span.status}`",
        f"- Duration: `{format_duration_seconds(duration_seconds(trace.root_span))}`",
        f"- Flows rendered: `{len(selected_flow_ids)}`",
        f"- Tasks rendered: `{len(selected_task_ids)}`",
        f"- Documents loaded: `{len(trace.documents)}`",
        f"- Total cost: `${float(totals['cost_usd']):.4f}`",
        f"- Tokens: `{int(totals['tokens_input']) + int(totals['tokens_output']) + int(totals['tokens_cache_read']) + int(totals['tokens_reasoning'])}`",
        "",
        "## Failures",
        "",
    ]
    if not failed_tasks:
        lines.append("No failed tasks in the rendered selection.")
        lines.append("")
    else:
        lines.extend(["| Flow | Task | Error | Link |", "| --- | --- | --- | --- |"])
        for task in failed_tasks:
            error_text = task.span.error_message or task.span.error_type or "failed"
            lines.append(
                f"| {display_flow_name(trace, task.parent_flow_span)} | {task.span.name} | {error_text} | [task]({task_file_map[task.span.span_id]}) |"
            )
        lines.append("")

    lines.extend([
        "## Flows",
        "",
        "| Flow | Status | Tasks | Duration | Cost | Link |",
        "| --- | --- | ---: | --- | ---: | --- |",
    ])
    for flow_span in trace.flow_spans:
        if flow_span.span_id not in selected_flow_ids:
            continue
        task_count = sum(1 for task_id in trace.tasks_by_flow.get(flow_span.span_id, ()) if task_id in selected_task_ids)
        flow_dur = format_duration_seconds(duration_seconds(flow_span))
        fc = flow_cost(trace, flow_span.span_id, selected_task_ids)
        lines.append(
            f"| {display_flow_name(trace, flow_span)} | `{flow_span.status}` | {task_count} | "
            f"{flow_dur} | ${fc:.4f} | [flow]({flow_file_map[flow_span.span_id]}) |"
        )
    lines.extend([
        "",
        "## Documents",
        "",
        "All canonical documents live in `docs/`. Task pages reference them by short filename only.",
    ])
    if collapsed_task_row_count > MAX_INDEX_TASK_ROWS:
        lines.extend(["", "## Task Map", "", "Omitted here because the run is large. Use each `flow.md` file for the detailed task table."])
        return "\n".join(lines).strip() + "\n"
    lines.extend(["", "## Task Map", ""])
    for flow_span in trace.flow_spans:
        if flow_span.span_id not in selected_flow_ids:
            continue
        lines.extend(_index_flow_task_table(trace, flow_span.span_id, selected_task_ids, task_file_map, batch_threshold))
    return "\n".join(lines).strip() + "\n"


def render_flow_markdown(
    trace: LoadedTrace,
    flow_id: UUID,
    task_file_map: dict[UUID, str],
    selected_task_ids: set[UUID],
    batch_threshold: int,
) -> str:
    """Render one flow summary markdown file."""
    flow_span = trace.spans_by_id[flow_id]
    lines = [
        f"# Flow: {display_flow_name(trace, flow_span)}",
        "",
        f"- Span ID: `{flow_span.span_id}`",
        f"- Status: `{flow_span.status}`",
        f"- Sequence: `{flow_span.sequence_no}`",
        f"- Duration: `{format_duration_seconds(duration_seconds(flow_span))}`",
        f"- Cost: `${flow_cost(trace, flow_id, selected_task_ids):.4f}`",
        "",
        "## Tasks",
        "",
        "| # | Task | Status | Duration | Cost | Link |",
        "| ---: | --- | --- | --- | ---: | --- |",
    ]
    for task_index, depth, task_ids in _flow_task_rows(trace, flow_id, selected_task_ids, batch_threshold):
        label = task_row_label(trace, task_ids, depth)
        task_link = task_file_map[task_ids[0]].split("/", 2)[-1]
        lines.append(
            f"| {task_index} | {label} | `{task_row_status(trace, task_ids)}` | "
            f"{format_duration_seconds(task_row_duration_seconds(trace, task_ids))} | "
            f"${task_row_cost(trace, task_ids):.4f} | [task]({task_link}) |"
        )
    return "\n".join(lines).strip() + "\n"


def render_task_markdown(
    trace: LoadedTrace,
    task: LoadedTask,
    provenance: dict[str, ProvenanceEntry],
    *,
    task_file_map: dict[UUID, str],
    flow_file_map: dict[UUID, str],
    config: RenderConfig,
) -> str:
    """Render one task markdown file."""
    lines = [f"# Task: {task.span.name}", "", *_task_metadata_lines(trace, task)]
    lines.extend(_task_body_lines(trace, task, provenance, task_file_map=task_file_map, flow_file_map=flow_file_map, config=config, include_related_files=True))
    return "\n".join(lines).strip() + "\n"


def render_batch_markdown(
    trace: LoadedTrace,
    tasks: tuple[LoadedTask, ...],
    provenance: dict[str, ProvenanceEntry],
    *,
    task_file_map: dict[UUID, str],
    flow_file_map: dict[UUID, str],
    config: RenderConfig,
) -> str:
    """Render one collapsed batch markdown file."""
    first_task = tasks[0]
    batch_path = task_file_map[first_task.span.span_id]
    batch_task_ids = tuple(task.span.span_id for task in tasks)
    lines = [
        f"# Batch: {first_task.span.name} (x{len(tasks)})",
        "",
        f"- Flow: `{display_flow_name(trace, first_task.parent_flow_span)}`",
        f"- Instances: `{len(tasks)}`",
        f"- Status: `{task_row_status(trace, batch_task_ids)}`",
        f"- Duration: `{format_duration_seconds(task_row_duration_seconds(trace, batch_task_ids))}`",
        f"- Cost: `${task_row_cost(trace, batch_task_ids):.4f}`",
        f"- Tokens: `{task_row_token_summary(trace, batch_task_ids)}`",
        f"- Flow summary: [flow.md]({relative_link(batch_path, flow_file_map[first_task.parent_flow_span.span_id])})",
        "",
        "## Instances",
        "",
        "| # | Span ID | Status | Duration | Cost | Tokens |",
        "| ---: | --- | --- | --- | ---: | --- |",
    ]
    for instance_index, task in enumerate(tasks, start=1):
        lines.append(
            f"| {instance_index} | `{task.span.span_id}` | `{task.span.status}` | "
            f"{format_duration_seconds(task.duration_seconds)} | ${task.descendant_cost_usd:.4f} | {task_token_summary(task)} |"
        )
    lines.append("")
    lines.extend(
        _batch_task_section(
            trace, tasks[0], provenance, task_file_map=task_file_map, flow_file_map=flow_file_map, config=config, heading="Representative Instance"
        )
    )
    for instance_index, task in enumerate(tasks[1:], start=2):
        if task.span.status != "failed":
            continue
        lines.extend(
            _batch_task_section(
                trace, task, provenance, task_file_map=task_file_map, flow_file_map=flow_file_map, config=config, heading=f"Failed Instance {instance_index}"
            )
        )
    return "\n".join(lines).strip() + "\n"


def render_document_file(document: LoadedDocument, provenance: ProvenanceEntry | None, config: RenderConfig) -> str:
    """Render one canonical document markdown file."""
    lines = [
        f"# Document: {document.short_id}",
        "",
        f"- Name: `{document.record.name}`",
        f"- Type: `{document.record.document_type}`",
        f"- MIME: `{document.record.mime_type}`",
        f"- Size: `{document.record.size_bytes}` bytes",
        f"- Produced by: `{provenance.produced_by_label if provenance is not None else 'unknown'}`",
        "",
        "## Provenance",
        "",
    ]
    lines.extend(_document_provenance_lines(provenance))
    lines.extend(_document_content_lines(document))
    if document.attachments:
        lines.extend(_document_attachment_lines(document, config))
    return "\n".join(lines).strip() + "\n"


def render_comparison_markdown(
    left_task: LoadedTask,
    right_task: LoadedTask,
    left_trace: LoadedTrace,
    right_trace: LoadedTrace,
    config: RenderConfig,
) -> str:
    """Render one task comparison markdown file."""
    left_documents = {left_trace.documents[sha].output_filename for sha in left_task.input_document_shas if sha in left_trace.documents}
    right_documents = {right_trace.documents[sha].output_filename for sha in right_task.input_document_shas if sha in right_trace.documents}
    left_transcript = _comparison_transcript(left_task, left_trace, config)
    right_transcript = _comparison_transcript(right_task, right_trace, config)
    lines = [
        f"# Comparison: {left_task.span.name}",
        "",
        "## Summary",
        "",
        "| Side | Status | Duration | Cost | Task Span |",
        "| --- | --- | --- | ---: | --- |",
        f"| Original | `{left_task.span.status}` | {format_duration_seconds(left_task.duration_seconds)} | "
        f"${left_task.descendant_cost_usd:.4f} | `{left_task.span.span_id}` |",
        f"| Replay | `{right_task.span.status}` | {format_duration_seconds(right_task.duration_seconds)} | "
        f"${right_task.descendant_cost_usd:.4f} | `{right_task.span.span_id}` |",
        "",
        "## Inputs",
        "",
        f"- Original: `{', '.join(sorted(left_documents)) or 'none'}`",
        f"- Replay: `{', '.join(sorted(right_documents)) or 'none'}`",
        f"- Identical: `{left_documents == right_documents}`",
        "",
        "## Original",
        "",
        left_transcript or "No recorded transcript.",
        "",
        "## Replay",
        "",
        right_transcript or "No recorded transcript.",
    ]
    return "\n".join(lines).strip() + "\n"


# --- Private helpers ---


def _task_metadata_lines(trace: LoadedTrace, task: LoadedTask) -> list[str]:
    lines = [
        f"- Span ID: `{task.span.span_id}`",
        f"- Flow: `{display_flow_name(trace, task.parent_flow_span)}`",
        f"- Status: `{task.span.status}`",
        f"- Duration: `{format_duration_seconds(task.duration_seconds)}`",
        f"- Cost: `${task.descendant_cost_usd:.4f}`",
        f"- Tokens: `{task_token_summary(task)}`",
    ]
    if task.span.status == "failed":
        lines.append(f"- Error: `{task.span.error_message or task.span.error_type or 'unknown'}`")
    lines.append("")
    return lines


def _task_body_lines(
    trace: LoadedTrace,
    task: LoadedTask,
    provenance: dict[str, ProvenanceEntry],
    *,
    task_file_map: dict[UUID, str],
    flow_file_map: dict[UUID, str],
    config: RenderConfig,
    include_related_files: bool,
) -> list[str]:
    documents_by_short_id = {document.short_id: document for document in trace.documents.values()}
    input_documents, output_documents, head_chars, tail_chars = _task_documents(trace, task, config)
    lines = _task_information_flow(input_documents, output_documents, provenance)
    lines.extend(_task_source_documents(input_documents, provenance, config, head_chars, tail_chars))
    lines.extend(_task_conversations(task, documents_by_short_id, config))
    lines.extend(_task_subtasks(trace, task, task_file_map))
    lines.extend(_task_output_documents(output_documents, provenance, config, head_chars, tail_chars))
    lines.extend(_task_diagnosis(task))
    if include_related_files:
        lines.extend(_task_related_files(trace, task, task_file_map, flow_file_map))
    return lines


def _batch_task_section(
    trace: LoadedTrace,
    task: LoadedTask,
    provenance: dict[str, ProvenanceEntry],
    *,
    task_file_map: dict[UUID, str],
    flow_file_map: dict[UUID, str],
    config: RenderConfig,
    heading: str,
) -> list[str]:
    lines = [f"## {heading}", "", *_task_metadata_lines(trace, task)]
    lines.extend(
        _task_body_lines(trace, task, provenance, task_file_map=task_file_map, flow_file_map=flow_file_map, config=config, include_related_files=False)
    )
    return lines


def _task_documents(trace: LoadedTrace, task: LoadedTask, config: RenderConfig) -> tuple[list[LoadedDocument], list[LoadedDocument], int, int]:
    from trace_inspector._truncation import truncation_profile_for_task

    input_documents = [trace.documents[sha] for sha in task.input_document_shas if sha in trace.documents]
    output_documents = [trace.documents[sha] for sha in task.output_document_shas if sha in trace.documents]
    large_input_count = sum(1 for doc in input_documents if not should_inline_document(doc.record.size_bytes, config))
    head_chars, tail_chars = truncation_profile_for_task(large_input_count, config)
    return input_documents, output_documents, head_chars, tail_chars


def _task_information_flow(
    input_documents: list[LoadedDocument],
    output_documents: list[LoadedDocument],
    provenance: dict[str, ProvenanceEntry],
) -> list[str]:
    lines = ["## Information Flow", "", "### Inputs", ""]
    if not input_documents:
        lines.extend(["No input documents.", ""])
    else:
        lines.extend(["| Document | Type | Size | Produced By |", "| --- | --- | ---: | --- |"])
        for document in input_documents:
            produced_by = provenance.get(document.record.document_sha256)
            label = produced_by.produced_by_label if produced_by is not None else "unknown"
            lines.append(f"| {doc_link(document)} | {document.record.document_type} | {document.record.size_bytes} | {label} |")
        lines.append("")
    lines.extend(["### Outputs", ""])
    if not output_documents:
        lines.extend(["No output documents.", ""])
    else:
        lines.extend(["| Document | Type | Size | Consumed By |", "| --- | --- | ---: | --- |"])
        for document in output_documents:
            entry = provenance.get(document.record.document_sha256)
            consumed_by = format_label_summary(entry.consumed_by_labels) if entry is not None and entry.consumed_by_labels else "not consumed in-run"
            lines.append(f"| {doc_link(document)} | {document.record.document_type} | {document.record.size_bytes} | {consumed_by} |")
        lines.append("")
    return lines


def _task_source_documents(
    input_documents: list[LoadedDocument],
    provenance: dict[str, ProvenanceEntry],
    config: RenderConfig,
    head_chars: int,
    tail_chars: int,
) -> list[str]:
    lines = ["## Source Documents", ""]
    if not input_documents:
        return [*lines, "No source documents.", ""]
    for document in input_documents:
        lines.extend(
            render_task_document_block(document, provenance.get(document.record.document_sha256), head_chars=head_chars, tail_chars=tail_chars, config=config)
        )
    return lines


def _task_conversations(task: LoadedTask, documents_by_short_id: dict[str, LoadedDocument], config: RenderConfig) -> list[str]:
    lines = ["## Conversations", ""]
    if not task.conversation_spans:
        return [*lines, "No recorded conversations.", ""]
    for conversation_index, conversation in enumerate(task.conversation_spans, start=1):
        lines.extend([f"### Conversation {conversation_index}: {conversation.name}", ""])
        llm_rounds = task.llm_rounds_by_conversation.get(conversation.span_id, ())
        tool_calls = task.tool_calls_by_conversation.get(conversation.span_id, ())
        lines.extend([conversation_metadata_line(llm_rounds), ""])
        transcript = render_message_sequence(llm_rounds, tool_calls, documents_by_short_id, config)
        lines.extend([transcript or "No recorded request/response messages.", ""])
    return lines


def _task_subtasks(trace: LoadedTrace, task: LoadedTask, task_file_map: dict[UUID, str]) -> list[str]:
    if not task.child_task_ids:
        return []
    lines = ["## Sub-Tasks", ""]
    child_task_groups = _group_linked_subtasks(trace, task.child_task_ids, task_file_map)
    for child_task_ids in child_task_groups:
        child_task = trace.tasks[child_task_ids[0]]
        label = child_task.span.name
        if len(child_task_ids) > 1:
            label = f"{label} (x{len(child_task_ids)})"
        lines.append(f"- [{label}]({relative_link(task_file_map[task.span.span_id], task_file_map[child_task_ids[0]])})")  # pyright: ignore[reportGeneralTypeIssues] — groups are non-empty by construction
    lines.append("")
    return lines


def _task_output_documents(
    output_documents: list[LoadedDocument],
    provenance: dict[str, ProvenanceEntry],
    config: RenderConfig,
    head_chars: int,
    tail_chars: int,
) -> list[str]:
    lines = ["## Output Documents", ""]
    if not output_documents:
        return [*lines, "No output documents.", ""]
    for document in output_documents:
        lines.extend(
            render_task_document_block(document, provenance.get(document.record.document_sha256), head_chars=head_chars, tail_chars=tail_chars, config=config)
        )
    return lines


def _task_diagnosis(task: LoadedTask) -> list[str]:
    if task.span.status != "failed":
        return []
    return ["## Diagnosis", "", task.span.error_message or task.span.error_type or "Task failed without an error message in the stored span record.", ""]


def _task_related_files(
    trace: LoadedTrace,
    task: LoadedTask,
    task_file_map: dict[UUID, str],
    flow_file_map: dict[UUID, str],
) -> list[str]:
    current_task_path = task_file_map[task.span.span_id]
    related_paths: list[str] = []
    if task.parent_task_id is not None and task.parent_task_id in task_file_map:
        parent_path = task_file_map[task.parent_task_id]
        related_paths.append(f"- Parent task: [{trace.tasks[task.parent_task_id].span.name}]({relative_link(current_task_path, parent_path)})")
    sibling_lines = _sibling_navigation_lines(trace, task, task_file_map)
    return [
        "## Related Files",
        "",
        f"- Flow summary: [flow.md]({relative_link(current_task_path, flow_file_map[task.parent_flow_span.span_id])})",
        *sibling_lines,
        *related_paths,
    ]


def _comparison_transcript(task: LoadedTask, trace: LoadedTrace, config: RenderConfig) -> str:
    """Render all conversations for a task in comparison mode."""
    if not task.conversation_spans:
        return ""
    documents_by_short_id = {document.short_id: document for document in trace.documents.values()}
    parts: list[str] = []
    for conversation_index, conversation in enumerate(task.conversation_spans, start=1):
        if len(task.conversation_spans) > 1:
            parts.append(f"### Conversation {conversation_index}: {conversation.name}")
            parts.append("")
        transcript = render_message_sequence(
            task.llm_rounds_by_conversation.get(conversation.span_id, ()),
            task.tool_calls_by_conversation.get(conversation.span_id, ()),
            documents_by_short_id,
            config,
        )
        parts.append(transcript or "No recorded request/response messages.")
        parts.append("")
    return "\n".join(parts).strip()


def _document_provenance_lines(provenance: ProvenanceEntry | None) -> list[str]:
    if provenance is None:
        return ["No provenance information available.", ""]
    return [
        f"- Produced by: `{provenance.produced_by_label}`",
        f"- Consumed by: `{format_label_summary(provenance.consumed_by_labels)}`",
        f"- Derived from: `{', '.join(provenance.derived_from_labels) or 'none'}`",
        f"- Triggered by: `{', '.join(provenance.triggered_by_labels) or 'none'}`",
        f"- External sources: `{', '.join(provenance.external_source_labels) or 'none'}`",
        "",
    ]


def _document_content_lines(document: LoadedDocument) -> list[str]:
    lines = ["## Content", ""]
    if document.text_content is not None:
        lines.extend([f"```{fence_language(document.record.name)}", document.text_content, "```", ""])
        return lines
    lines.extend([binary_notice(name=document.record.name, mime_type=document.record.mime_type, size_bytes=document.record.size_bytes), ""])
    return lines


def _document_attachment_lines(document: LoadedDocument, config: RenderConfig) -> list[str]:
    lines = ["## Attachments", ""]
    for attachment in document.attachments:
        lines.extend([f"### {attachment.name}", ""])
        if attachment.text_content is None:
            lines.append(
                binary_notice(
                    name=attachment.name, mime_type=attachment.mime_type, size_bytes=attachment.size_bytes, filename=f"{document.output_filename} attachments"
                )
            )
            lines.append("")
            continue
        attachment_text = preview_or_truncate_attachment(
            attachment.text_content,
            size_bytes=attachment.size_bytes,
            filename=f"{document.output_filename} attachments",
            config=config,
            head_chars=config.head_chars,
            tail_chars=config.tail_chars,
        )
        lines.extend([f"```{fence_language(attachment.name)}", attachment_text, "```"])
        lines.append("")
    return lines


def _group_linked_subtasks(trace: LoadedTrace, child_task_ids: tuple[UUID, ...], task_file_map: dict[UUID, str]) -> list[tuple[UUID, ...]]:
    groups: list[tuple[UUID, ...]] = []
    index = 0
    while index < len(child_task_ids):
        current_task_id = child_task_ids[index]
        current_path = task_file_map.get(current_task_id)
        run_ids = [current_task_id]
        cursor = index + 1
        while cursor < len(child_task_ids):
            candidate_task_id = child_task_ids[cursor]
            if task_file_map.get(candidate_task_id) != current_path:
                break
            if trace.tasks[candidate_task_id].span.name != trace.tasks[current_task_id].span.name:
                break
            run_ids.append(candidate_task_id)
            cursor += 1
        groups.append(tuple(run_ids))
        index = cursor
    return groups


def _index_flow_task_table(
    trace: LoadedTrace,
    flow_id: UUID,
    selected_task_ids: set[UUID],
    task_file_map: dict[UUID, str],
    batch_threshold: int,
) -> list[str]:
    flow_span = trace.spans_by_id[flow_id]
    lines = [f"### {display_flow_name(trace, flow_span)}", "", "| # | Task | Status | Duration | Cost | Link |", "| ---: | --- | --- | --- | ---: | --- |"]
    for row_index, depth, task_ids in _flow_task_rows(trace, flow_id, selected_task_ids, batch_threshold):
        label = task_row_label(trace, task_ids, depth)
        lines.append(
            f"| {row_index} | {label} | `{task_row_status(trace, task_ids)}` | "
            f"{format_duration_seconds(task_row_duration_seconds(trace, task_ids))} | "
            f"${task_row_cost(trace, task_ids):.4f} | [task]({task_file_map[task_ids[0]]}) |"
        )
    lines.append("")
    return lines


def _flow_task_rows(trace: LoadedTrace, flow_id: UUID, selected_task_ids: set[UUID], batch_threshold: int) -> list[FlowTaskRow]:
    rows: list[FlowTaskRow] = []
    _append_task_rows(
        trace=trace,
        task_ids=root_task_ids(trace, flow_id, selected_task_ids),
        selected_task_ids=selected_task_ids,
        batch_threshold=batch_threshold,
        rows=rows,
        row_index=1,
        depth=0,
    )
    return rows


def _append_task_rows(
    *,
    trace: LoadedTrace,
    task_ids: tuple[UUID, ...],
    selected_task_ids: set[UUID],
    batch_threshold: int,
    rows: list[FlowTaskRow],
    row_index: int,
    depth: int,
) -> int:
    next_index = row_index
    for _, grouped_task_ids in build_batch_groups(trace, task_ids, selected_task_ids=selected_task_ids, batch_threshold=batch_threshold):
        rows.append((next_index, depth, grouped_task_ids))
        next_index += 1
        if len(grouped_task_ids) != 1:
            continue
        child_task_ids = tuple(child_id for child_id in trace.tasks[grouped_task_ids[0]].child_task_ids if child_id in selected_task_ids)
        if not child_task_ids:
            continue
        next_index = _append_task_rows(
            trace=trace,
            task_ids=child_task_ids,
            selected_task_ids=selected_task_ids,
            batch_threshold=batch_threshold,
            rows=rows,
            row_index=next_index,
            depth=depth + 1,
        )
    return next_index


def _sibling_navigation_lines(trace: LoadedTrace, task: LoadedTask, task_file_map: dict[UUID, str]) -> list[str]:
    current_task_path = task_file_map[task.span.span_id]
    sibling_task_ids = _sibling_task_ids(trace, task)
    if task.span.span_id not in sibling_task_ids:
        return []
    current_index = sibling_task_ids.index(task.span.span_id)
    lines: list[str] = []
    if current_index > 0:
        previous_task_id = sibling_task_ids[current_index - 1]
        previous_path = task_file_map[previous_task_id]
        if previous_path != current_task_path:
            lines.append(f"- Previous task: [{trace.tasks[previous_task_id].span.name}]({relative_link(current_task_path, previous_path)})")
    if current_index < len(sibling_task_ids) - 1:
        next_task_id = sibling_task_ids[current_index + 1]
        next_path = task_file_map[next_task_id]
        if next_path != current_task_path:
            lines.append(f"- Next task: [{trace.tasks[next_task_id].span.name}]({relative_link(current_task_path, next_path)})")
    return lines


def _sibling_task_ids(trace: LoadedTrace, task: LoadedTask) -> list[UUID]:
    if task.parent_task_id is not None and task.parent_task_id in trace.tasks:
        return list(trace.tasks[task.parent_task_id].child_task_ids)
    return [candidate_id for candidate_id in trace.tasks_by_flow.get(task.parent_flow_span.span_id, ()) if trace.tasks[candidate_id].parent_task_id is None]
