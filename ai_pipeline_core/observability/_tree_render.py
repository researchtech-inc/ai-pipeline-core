"""Terminal rendering helpers for span execution trees.

Provides plain-text formatting for ``ai-trace show`` output. Data computation
lives in ``database.snapshot._spans``; this module handles presentation only.
"""

from datetime import timedelta
from typing import Any
from uuid import UUID

from ai_pipeline_core.database._types import SpanKind, SpanRecord, SpanStatus
from ai_pipeline_core.database.filesystem._paths import span_filename
from ai_pipeline_core.database.snapshot._spans import (
    CACHE_HIT_KEY,
    MODEL_KEY,
    PURPOSE_KEY,
    ROUND_INDEX_KEY,
    STEP_KEY,
    TOKENS_CACHE_READ_KEY,
    TOKENS_INPUT_KEY,
    TOKENS_OUTPUT_KEY,
    TOKENS_REASONING_KEY,
    TOOL_NAME_KEY,
    TOTAL_STEPS_KEY,
    SpanTreeView,
    _detail_bool,
    _detail_int,
    _detail_str,
    _TokenTotals,
)

__all__ = [
    "format_span_overview_lines",
    "format_span_tree_lines",
]

_FOUR_SPACES = "    "
MAX_TREE_DEPTH = 100
UNKNOWN_MODEL_LABEL = "(unknown)"


def _format_timedelta(delta: timedelta) -> str:
    secs = delta.total_seconds()
    if secs < 1:
        return f"{int(secs * 1000)}ms"
    if secs < 60:
        return f"{secs:.1f}s" if secs < 10 else f"{secs:.0f}s"
    if secs < 3600:
        return f"{int(secs // 60)}m {int(secs % 60)}s"
    return f"{int(secs // 3600)}h {int((secs % 3600) // 60)}m"


def _format_duration(span: SpanRecord) -> str:
    if span.ended_at is None:
        return "running..." if span.status == SpanStatus.RUNNING else "-"
    return _format_timedelta(span.ended_at - span.started_at)


def _format_token_count(count: int) -> str:
    if count >= 1000:
        return f"{round(count / 1000)}K"
    return str(count)


def _format_token_parts(span: SpanRecord, metrics: dict[str, Any], view: SpanTreeView) -> str:
    if span.kind not in {SpanKind.CONVERSATION, SpanKind.PROMPT_EXECUTION, SpanKind.LLM_ROUND}:
        return ""
    if span.kind in {SpanKind.CONVERSATION, SpanKind.PROMPT_EXECUTION}:
        dt = view.descendant_tokens.get(span.span_id, _TokenTotals())
        tokens_input, tokens_output = dt.tokens_input, dt.tokens_output
        tokens_cache_read, tokens_reasoning = dt.tokens_cache_read, dt.tokens_reasoning
    else:
        context = f"Span {span.span_id}"
        tokens_input = _detail_int(metrics, TOKENS_INPUT_KEY, context=context, field_name="metrics_json")
        tokens_output = _detail_int(metrics, TOKENS_OUTPUT_KEY, context=context, field_name="metrics_json")
        tokens_cache_read = _detail_int(metrics, TOKENS_CACHE_READ_KEY, context=context, field_name="metrics_json")
        tokens_reasoning = _detail_int(metrics, TOKENS_REASONING_KEY, context=context, field_name="metrics_json")
    parts = [f"{_format_token_count(tokens_input)} in"]
    if tokens_cache_read > 0:
        parts.append(f"{_format_token_count(tokens_cache_read)} cache")
    parts.append(f"{_format_token_count(tokens_output)} out")
    if tokens_reasoning > 0:
        parts.append(f"{_format_token_count(tokens_reasoning)} reasoning")
    return " / ".join(parts)


def _cache_hit_suffix(meta: dict[str, Any]) -> str:
    if _detail_bool(meta, CACHE_HIT_KEY):
        return " cache-hit"
    return ""


def _conversation_label(span: SpanRecord, meta: dict[str, Any]) -> str:
    purpose = _detail_str(meta, PURPOSE_KEY)
    if purpose:
        return purpose
    if span.description:
        return span.description
    return span.name


def _flow_step_label(span: SpanRecord, meta: dict[str, Any]) -> str:
    context = f"Span {span.span_id}"
    step = _detail_int(meta, STEP_KEY, context=context, field_name="meta_json")
    total_steps = _detail_int(meta, TOTAL_STEPS_KEY, context=context, field_name="meta_json")
    if step > 0 and total_steps > 0:
        return f"[{step}/{total_steps}]"
    if step > 0:
        return f"[{step}]"
    if span.sequence_no > 0:
        return f"[{span.sequence_no}]"
    return ""


def _round_label(span: SpanRecord, meta: dict[str, Any]) -> str:
    context = f"Span {span.span_id}"
    round_index = _detail_int(meta, ROUND_INDEX_KEY, context=context, field_name="meta_json")
    if round_index > 0:
        return f"[{round_index}]"
    if span.sequence_no > 0:
        return f"[{span.sequence_no}]"
    return ""


def _span_local_filename(span: SpanRecord, view: SpanTreeView) -> str | None:
    if span.kind in {SpanKind.LLM_ROUND, SpanKind.TOOL_CALL, SpanKind.ATTEMPT}:
        return None
    if span.kind == SpanKind.DEPLOYMENT and span.parent_span_id is None:
        return "deployment.json"
    if span.parent_span_id is None:
        return None
    siblings = [
        sibling_id
        for sibling_id in view.children_map.get(span.parent_span_id, [])
        if view.spans_by_id[sibling_id].kind not in {SpanKind.LLM_ROUND, SpanKind.TOOL_CALL, SpanKind.ATTEMPT}
    ]
    sibling_index = siblings.index(span.span_id) + 1
    return span_filename(span.kind, span.name, span.span_id, sibling_index)


def _format_attempt_line(span: SpanRecord, meta: dict[str, Any], view: SpanTreeView, *, indent: str, status: str, duration: str) -> str:
    context = f"Span {span.span_id}"
    attempt_num = _detail_int(meta, "attempt", context=context, field_name="meta_json")
    max_attempts = _detail_int(meta, "max_attempts", context=context, field_name="meta_json")
    label = f"{attempt_num}/{max_attempts}" if max_attempts > 0 else str(attempt_num)
    rendered = f"{indent}attempt: {label} {status} {duration}"
    cost = span.cost_usd + view.descendant_costs.get(span.span_id, 0.0)
    if cost > 0:
        rendered += f" ${cost:.4f}"
    return rendered


def _format_conversation_like_line(
    span: SpanRecord,
    view: SpanTreeView,
    meta: dict[str, Any],
    metrics: dict[str, Any],
    *,
    indent: str,
    duration: str,
    cache_suffix: str,
    include_filenames: bool,
    label_prefix: str,
    chain_suffix: str,
) -> str:
    model = _detail_str(meta, MODEL_KEY) or UNKNOWN_MODEL_LABEL
    tokens = _format_token_parts(span, metrics, view)
    cost = span.cost_usd + view.descendant_costs.get(span.span_id, 0.0)
    line = f"{indent}{label_prefix}: {_conversation_label(span, meta)} {duration} {model}{chain_suffix}"
    if tokens:
        line += f" {tokens}"
    if cost > 0:
        line += f" ${cost:.4f}"
    rendered = f"{line}{cache_suffix}"
    filename = _span_local_filename(span, view) if include_filenames else None
    return f"{rendered}  -> {filename}" if filename is not None else rendered


def _format_tree_line(span: SpanRecord, view: SpanTreeView, *, depth: int, include_filenames: bool) -> str:
    indent = _FOUR_SPACES * depth
    meta = view.meta_by_id[span.span_id]
    metrics = view.metrics_by_id[span.span_id]
    status = str(span.status)
    duration = _format_duration(span)
    cache_suffix = _cache_hit_suffix(meta)

    if span.kind in {SpanKind.CONVERSATION, SpanKind.PROMPT_EXECUTION}:
        chain_suffix = f" (continues {str(span.previous_conversation_id)[:8]}…)" if span.kind == SpanKind.CONVERSATION and span.previous_conversation_id else ""
        label_prefix = "conversation" if span.kind == SpanKind.CONVERSATION else "prompt_exec"
        return _format_conversation_like_line(
            span,
            view,
            meta,
            metrics,
            indent=indent,
            duration=duration,
            cache_suffix=cache_suffix,
            include_filenames=include_filenames,
            label_prefix=label_prefix,
            chain_suffix=chain_suffix,
        )

    if span.kind == SpanKind.LLM_ROUND:
        model = _detail_str(meta, MODEL_KEY) or UNKNOWN_MODEL_LABEL
        tokens = _format_token_parts(span, metrics, view)
        line = f"{indent}llm_round{_round_label(span, meta)}: {model} {duration}"
        if tokens:
            line += f" {tokens}"
        if span.cost_usd > 0:
            line += f" ${span.cost_usd:.4f}"
        rendered = f"{line}{cache_suffix}"
        filename = _span_local_filename(span, view) if include_filenames else None
        return f"{rendered}  -> {filename}" if filename is not None else rendered

    if span.kind == SpanKind.TOOL_CALL:
        tool_name = _detail_str(meta, TOOL_NAME_KEY) or span.name
        return f"{indent}tool_call{_round_label(span, meta)}: {tool_name} {status} {duration}{cache_suffix}"

    if span.kind == SpanKind.FLOW:
        step_label = _flow_step_label(span, meta)
        rendered = f"{indent}flow{step_label}: {span.name} {status} {duration}{cache_suffix}"
        filename = _span_local_filename(span, view) if include_filenames else None
        return f"{rendered}  -> {filename}" if filename is not None else rendered

    if span.kind == SpanKind.ATTEMPT:
        return _format_attempt_line(span, meta, view, indent=indent, status=status, duration=duration)

    rendered = f"{indent}{span.kind}: {span.name} {status} {duration}{cache_suffix}"
    filename = _span_local_filename(span, view) if include_filenames else None
    return f"{rendered}  -> {filename}" if filename is not None else rendered


def format_span_tree_lines(view: SpanTreeView, *, include_filenames: bool = False) -> list[str]:
    """Render the full span execution tree as indented plain-text lines."""
    lines: list[str] = []
    visited: set[UUID] = set()

    def append_tree_lines(span_id: UUID, depth: int) -> None:
        if span_id in visited:
            lines.append(f"{_FOUR_SPACES * depth}[cycle detected while rendering execution tree]")
            return
        if depth > MAX_TREE_DEPTH:
            lines.append(f"{_FOUR_SPACES * depth}[execution tree depth limit reached]")
            return

        visited.add(span_id)
        span = view.spans_by_id[span_id]

        if span.kind == SpanKind.ATTEMPT:
            attempt_siblings = [c for c in view.children_map.get(span.parent_span_id, []) if view.spans_by_id[c].kind == SpanKind.ATTEMPT]
            if len(attempt_siblings) == 1 and span.status != SpanStatus.FAILED:
                # Single non-failed attempt — skip the ATTEMPT wrapper line and render children at same depth.
                # Failed attempts are always shown so timing and error context remain visible for debugging.
                for child_id in view.children_map.get(span_id, []):
                    append_tree_lines(child_id, depth)
                visited.remove(span_id)
                return

        lines.append(_format_tree_line(span, view, depth=depth, include_filenames=include_filenames))
        for child_id in view.children_map.get(span_id, []):
            append_tree_lines(child_id, depth + 1)
        visited.remove(span_id)

    append_tree_lines(view.root_span.span_id, 0)
    return lines


def format_span_overview_lines(view: SpanTreeView) -> list[str]:
    """Render a compact plain-text deployment overview for ai-trace show."""
    total_tokens = view.totals.tokens_input + view.totals.tokens_output + view.totals.tokens_cache_read + view.totals.tokens_reasoning
    return [
        f"Deployment {view.root_span.deployment_name or view.root_span.name} / {view.root_span.run_id}",
        (
            f"Status: {view.root_span.status}  Duration: {_format_duration(view.root_span)}  "
            f"Spans: {sum(view.counts_by_kind.values())}  Documents: {len(view.document_shas)}"
        ),
        (
            f"Flows: {view.counts_by_kind.get(SpanKind.FLOW, 0)}  "
            f"Tasks: {view.counts_by_kind.get(SpanKind.TASK, 0)}  "
            f"Attempts: {view.counts_by_kind.get(SpanKind.ATTEMPT, 0)}  "
            f"Operations: {view.counts_by_kind.get(SpanKind.OPERATION, 0)}  "
            f"Conversations: {view.counts_by_kind.get(SpanKind.CONVERSATION, 0)}  "
            f"Prompt Executions: {view.counts_by_kind.get(SpanKind.PROMPT_EXECUTION, 0)}  "
            f"LLM Rounds: {view.counts_by_kind.get(SpanKind.LLM_ROUND, 0)}  "
            f"Tool Calls: {view.counts_by_kind.get(SpanKind.TOOL_CALL, 0)}"
        ),
        f"Total Tokens: {total_tokens:,}  Total Cost: ${view.totals.cost_usd:.4f}",
    ]
