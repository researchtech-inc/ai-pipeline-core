"""Shared formatting and calculation helpers for markdown rendering."""

from __future__ import annotations

import posixpath
from collections import Counter
from pathlib import Path
from uuid import UUID

from ai_pipeline_core.database._types import SpanKind, SpanRecord, get_token_count
from trace_inspector._helpers import duration_seconds, parse_json_object
from trace_inspector._truncation import binary_notice, should_inline_document, truncate_content
from trace_inspector._types import LoadedDocument, LoadedTask, LoadedTrace, ProvenanceEntry, RenderConfig

__all__ = [
    "MILLISECONDS_PER_SECOND",
    "PROVENANCE_SUMMARY_GROUP_LIMIT",
    "display_flow_name",
    "doc_link",
    "fence_language",
    "flow_cost",
    "format_duration_seconds",
    "format_label_summary",
    "is_batchable_task",
    "preview_document_text",
    "preview_or_truncate_attachment",
    "relative_link",
    "render_task_document_block",
    "root_task_ids",
    "summarize_totals",
    "task_row_cost",
    "task_row_duration_seconds",
    "task_row_label",
    "task_row_status",
    "task_row_token_summary",
    "task_token_summary",
]

MILLISECONDS_PER_SECOND = 1000
SECONDS_PER_MINUTE = 60
MINUTES_PER_HOUR = 60
PROVENANCE_SUMMARY_GROUP_LIMIT = 5

TOKENS_INPUT_KEY = "tokens_input"
TOKENS_OUTPUT_KEY = "tokens_output"
TOKENS_CACHE_READ_KEY = "tokens_cache_read"
TOKENS_REASONING_KEY = "tokens_reasoning"


def format_duration_seconds(duration_secs: float | None) -> str:
    """Format a duration in seconds as a human-readable string."""
    if duration_secs is None:
        return "running"
    if duration_secs < SECONDS_PER_MINUTE:
        return f"{duration_secs:.1f}s"
    minutes = int(duration_secs // SECONDS_PER_MINUTE)
    seconds = int(duration_secs % SECONDS_PER_MINUTE)
    if minutes < MINUTES_PER_HOUR:
        return f"{minutes}m {seconds}s"
    hours = minutes // MINUTES_PER_HOUR
    remaining_minutes = minutes % MINUTES_PER_HOUR
    return f"{hours}h {remaining_minutes}m"


def display_flow_name(trace: LoadedTrace, flow_span: SpanRecord) -> str:
    """Return a disambiguated flow name when multiple flows share the same name."""
    same_name_flows = [flow for flow in trace.flow_spans if flow.name == flow_span.name]
    if len(same_name_flows) == 1:
        return flow_span.name
    sequence = same_name_flows.index(flow_span) + 1
    return f"{flow_span.name} (seq {sequence})"


def relative_link(from_file: str, to_file: str) -> str:
    """Compute a POSIX relative link between two bundle file paths."""
    return posixpath.relpath(to_file, posixpath.dirname(from_file))


def doc_link(document: LoadedDocument) -> str:
    """Return an inline markdown reference for a document."""
    return f"`{document.output_filename}`"


def fence_language(name: str) -> str:
    """Return a markdown fence language tag from a filename."""
    suffix = Path(name).suffix.lower()
    return {
        ".json": "json",
        ".md": "markdown",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".xml": "xml",
        ".html": "html",
        ".txt": "text",
    }.get(suffix, "text")


def task_token_summary(task: LoadedTask) -> str:
    """Format a single task's token counts as a human-readable summary."""
    return f"{task.tokens_input} in / {task.tokens_output} out / {task.tokens_cache_read} cache / {task.tokens_reasoning} reasoning"


def task_row_token_summary(trace: LoadedTrace, task_ids: tuple[UUID, ...]) -> str:
    """Format aggregated token counts for a group of task IDs."""
    totals = {"tokens_input": 0, "tokens_output": 0, "tokens_cache_read": 0, "tokens_reasoning": 0}
    for task_id in task_ids:
        task = trace.tasks[task_id]
        totals["tokens_input"] += task.tokens_input
        totals["tokens_output"] += task.tokens_output
        totals["tokens_cache_read"] += task.tokens_cache_read
        totals["tokens_reasoning"] += task.tokens_reasoning
    return f"{totals['tokens_input']} in / {totals['tokens_output']} out / {totals['tokens_cache_read']} cache / {totals['tokens_reasoning']} reasoning"


def task_row_label(trace: LoadedTrace, task_ids: tuple[UUID, ...], depth: int) -> str:
    """Format a task row label with optional batch count and indentation."""
    first_task = trace.tasks[task_ids[0]]
    label = first_task.span.name
    if len(task_ids) > 1:
        label = f"{label} (x{len(task_ids)})"
    return f"{'  ' * depth}{label}"


def task_row_status(trace: LoadedTrace, task_ids: tuple[UUID, ...]) -> str:
    """Format the aggregated status for a group of task IDs."""
    counts = Counter(trace.tasks[task_id].span.status for task_id in task_ids)
    if len(counts) == 1:
        status = next(iter(counts))
        if len(task_ids) == 1:
            return status
        return f"{status} x{len(task_ids)}"
    return " / ".join(f"{status} x{count}" for status, count in counts.most_common())


def task_row_duration_seconds(trace: LoadedTrace, task_ids: tuple[UUID, ...]) -> float | None:
    """Sum the durations of a group of task IDs."""
    durations = [trace.tasks[task_id].duration_seconds for task_id in task_ids]
    if any(d is None for d in durations):
        return None
    return sum(d for d in durations if d is not None)


def task_row_cost(trace: LoadedTrace, task_ids: tuple[UUID, ...]) -> float:
    """Sum the costs of a group of task IDs."""
    return sum(trace.tasks[task_id].descendant_cost_usd for task_id in task_ids)


def root_task_ids(trace: LoadedTrace, flow_id: UUID, selected_task_ids: set[UUID]) -> tuple[UUID, ...]:
    """Return root task IDs for a flow — tasks whose parent is not in the selection."""
    result: list[UUID] = []
    for task_id in trace.tasks_by_flow.get(flow_id, ()):
        if task_id not in selected_task_ids:
            continue
        parent_task_id = trace.tasks[task_id].parent_task_id
        if parent_task_id is None or parent_task_id not in selected_task_ids:
            result.append(task_id)
    return tuple(result)


def is_batchable_task(trace: LoadedTrace, task_id: UUID, selected_task_ids: set[UUID]) -> bool:
    """Return whether a task is a leaf task (no selected children) eligible for batching."""
    return not any(child_task_id in selected_task_ids for child_task_id in trace.tasks[task_id].child_task_ids)


def flow_cost(trace: LoadedTrace, flow_id: UUID, selected_task_ids: set[UUID]) -> float:
    """Sum costs of root tasks in a flow."""
    return sum(trace.tasks[task_id].descendant_cost_usd for task_id in root_task_ids(trace, flow_id, selected_task_ids))


def format_label_summary(labels: tuple[str, ...], *, max_groups: int = PROVENANCE_SUMMARY_GROUP_LIMIT) -> str:
    """Format a tuple of labels into a summary with grouping and counts."""
    if not labels:
        return "none"
    counts = Counter(labels)
    groups = [f"{label} (x{count})" if count > 1 else label for label, count in counts.most_common(max_groups)]
    remaining = len(counts) - len(groups)
    if remaining > 0:
        groups.append(f"...and {remaining} others")
    return ", ".join(groups)


def summarize_totals(trace: LoadedTrace) -> dict[str, float | int]:
    """Aggregate cost and token totals across the entire trace.

    Cost includes all spans. Token counts include LLM_ROUND spans only,
    matching the framework's aggregate_cost_totals behavior.
    """
    totals: dict[str, float | int] = {
        "cost_usd": 0.0,
        "tokens_input": 0,
        "tokens_output": 0,
        "tokens_cache_read": 0,
        "tokens_reasoning": 0,
    }
    for span in trace.spans_by_id.values():
        totals["cost_usd"] += span.cost_usd
        if span.kind != SpanKind.LLM_ROUND:
            continue
        metrics = parse_json_object(span.metrics_json, context=f"Span {span.span_id}")
        totals["tokens_input"] += get_token_count(metrics, TOKENS_INPUT_KEY)
        totals["tokens_output"] += get_token_count(metrics, TOKENS_OUTPUT_KEY)
        totals["tokens_cache_read"] += get_token_count(metrics, TOKENS_CACHE_READ_KEY)
        totals["tokens_reasoning"] += get_token_count(metrics, TOKENS_REASONING_KEY)
    return totals


def preview_document_text(document: LoadedDocument, *, config: RenderConfig, head_chars: int, tail_chars: int) -> str | None:
    """Return a text preview for a document, or None for binary content."""
    if document.text_content is None:
        return None
    if should_inline_document(document.record.size_bytes, config):
        return document.text_content
    return truncate_content(
        document.text_content,
        filename=document.output_filename,
        total_size_bytes=document.record.size_bytes,
        head_chars=head_chars,
        tail_chars=tail_chars,
    )


def preview_or_truncate_attachment(
    text: str,
    *,
    size_bytes: int,
    filename: str,
    config: RenderConfig,
    head_chars: int,
    tail_chars: int,
) -> str:
    """Return inline or truncated attachment text."""
    if should_inline_document(size_bytes, config):
        return text
    return truncate_content(
        text,
        filename=filename,
        total_size_bytes=size_bytes,
        head_chars=head_chars,
        tail_chars=tail_chars,
    )


def render_task_document_block(
    document: LoadedDocument,
    provenance: ProvenanceEntry | None,
    *,
    head_chars: int,
    tail_chars: int,
    config: RenderConfig,
) -> list[str]:
    """Render one document section for a task page (source or output)."""
    lines = [
        f"### {document.output_filename}",
        "",
        f"- Name: `{document.record.name}`",
        f"- Type: `{document.record.document_type}`",
        f"- Produced by: `{provenance.produced_by_label if provenance is not None else 'unknown'}`",
    ]
    if provenance is not None:
        lines.append(f"- Consumed by: `{format_label_summary(provenance.consumed_by_labels)}`")
        if provenance.derived_from_labels:
            lines.append(f"- Derived from: `{', '.join(provenance.derived_from_labels)}`")
        if provenance.triggered_by_labels:
            lines.append(f"- Triggered by: `{', '.join(provenance.triggered_by_labels)}`")
        if provenance.external_source_labels:
            lines.append(f"- External sources: `{', '.join(provenance.external_source_labels)}`")
    lines.append("")
    preview = preview_document_text(document, config=config, head_chars=head_chars, tail_chars=tail_chars)
    if preview is not None:
        lines.append(f"```{fence_language(document.record.name)}")
        lines.append(preview)
        lines.append("```")
    else:
        lines.append(
            binary_notice(
                name=document.record.name, mime_type=document.record.mime_type, size_bytes=document.record.size_bytes, filename=document.output_filename
            )
        )
    lines.append("")
    if document.attachments:
        lines.extend(["#### Attachments", ""])
        for attachment in document.attachments:
            lines.append(f"- `{attachment.name}`")
            if attachment.text_content is None:
                notice = binary_notice(
                    name=attachment.name,
                    mime_type=attachment.mime_type,
                    size_bytes=attachment.size_bytes,
                    filename=f"{document.output_filename} attachments",
                )
                lines.append(f"  {notice}")
                continue
            attachment_preview = preview_or_truncate_attachment(
                attachment.text_content,
                size_bytes=attachment.size_bytes,
                filename=f"{document.output_filename} attachments",
                config=config,
                head_chars=head_chars,
                tail_chars=tail_chars,
            )
            lines.extend([f"```{fence_language(attachment.name)}", attachment_preview, "```"])
        lines.append("")
    return lines


def conversation_metadata_line(llm_rounds: tuple[SpanRecord, ...]) -> str:
    """Render a one-line summary of model, tokens, duration, and cost for a conversation."""
    if not llm_rounds:
        return "Model: `unknown` | Tokens: 0 in / 0 out | Duration: — | Cost: $0.0000"
    models: list[str] = []
    conv_totals: dict[str, int | float] = {"tokens_input": 0, "tokens_output": 0, "tokens_cache_read": 0, "cost_usd": 0.0, "time_taken_ms": 0}
    for llm_round in llm_rounds:
        meta = parse_json_object(llm_round.meta_json, context=f"Span {llm_round.span_id}")
        metrics = parse_json_object(llm_round.metrics_json, context=f"Span {llm_round.span_id}")
        model = meta.get("model")
        if isinstance(model, str) and model not in models:
            models.append(model)
        conv_totals["tokens_input"] += get_token_count(metrics, TOKENS_INPUT_KEY)
        conv_totals["tokens_output"] += get_token_count(metrics, TOKENS_OUTPUT_KEY)
        conv_totals["tokens_cache_read"] += get_token_count(metrics, TOKENS_CACHE_READ_KEY)
        conv_totals["time_taken_ms"] += get_token_count(metrics, "time_taken_ms")
        cost_value = metrics.get("cost_usd", llm_round.cost_usd)
        if isinstance(cost_value, int | float):
            conv_totals["cost_usd"] += float(cost_value)
    dur = format_duration_seconds(
        int(conv_totals["time_taken_ms"]) / MILLISECONDS_PER_SECOND if conv_totals["time_taken_ms"] else duration_seconds(llm_rounds[-1])
    )
    model_label = ", ".join(models) or "unknown"
    return (
        f"Model: `{model_label}` | Tokens: {conv_totals['tokens_input']} in / {conv_totals['tokens_output']} out / "
        f"{conv_totals['tokens_cache_read']} cache | Duration: {dur} | Cost: ${float(conv_totals['cost_usd']):.4f}"
    )
