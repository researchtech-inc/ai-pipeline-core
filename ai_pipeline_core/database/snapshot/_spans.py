"""Span tree data computation: indexes, cost aggregation, and tree building."""

from collections import Counter
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from ai_pipeline_core.database._json_helpers import parse_json_object
from ai_pipeline_core.database._types import (
    TOKENS_CACHE_READ_KEY,
    TOKENS_INPUT_KEY,
    TOKENS_OUTPUT_KEY,
    TOKENS_REASONING_KEY,
    CostTotals,
    SpanKind,
    SpanRecord,
    aggregate_cost_totals,
)

__all__ = [
    "SpanTreeView",
    "build_span_tree_view",
]

MODEL_KEY = "model"
PURPOSE_KEY = "purpose"
CACHE_HIT_KEY = "cache_hit"
STEP_KEY = "step"
TOTAL_STEPS_KEY = "total_steps"
ROUND_INDEX_KEY = "round_index"
TOOL_NAME_KEY = "tool_name"
FLOW_PLAN_KEY = "flow_plan"

_SPAN_KIND_PRIORITY: dict[str, int] = {
    SpanKind.DEPLOYMENT: 0,
    SpanKind.FLOW: 1,
    SpanKind.TASK: 2,
    SpanKind.ATTEMPT: 3,
    SpanKind.OPERATION: 4,
    SpanKind.CONVERSATION: 5,
    SpanKind.PROMPT_EXECUTION: 5,
    SpanKind.LLM_ROUND: 6,
    SpanKind.TOOL_CALL: 7,
}


@dataclass(frozen=True, slots=True)
class _TokenTotals:
    """Aggregated token counts for descendant LLM rounds."""

    tokens_input: int = 0
    tokens_output: int = 0
    tokens_cache_read: int = 0
    tokens_reasoning: int = 0


@dataclass(frozen=True, slots=True)
class SpanTreeView:
    """Precomputed indexes and totals for one span deployment tree."""

    root_span: SpanRecord
    spans_by_id: dict[UUID, SpanRecord]
    children_map: dict[UUID | None, list[UUID]]
    meta_by_id: dict[UUID, dict[str, Any]]
    metrics_by_id: dict[UUID, dict[str, Any]]
    counts_by_kind: Counter[str]
    document_shas: set[str]
    descendant_costs: dict[UUID, float]
    descendant_tokens: dict[UUID, _TokenTotals]
    totals: CostTotals


def _detail_int(payload: dict[str, Any], key: str, *, context: str, field_name: str) -> int:
    value = payload.get(key, 0)
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError) as exc:  # fmt: skip
        msg = (
            f"{context} {field_name}[{key!r}] must be an integer value. Persist an integer before rendering summaries."
        )
        raise ValueError(msg) from exc


def _detail_str(payload: dict[str, Any], key: str) -> str:  # pyright: ignore[reportUnusedFunction] — imported by observability._tree_render
    value = payload.get(key, "")
    if isinstance(value, str):
        return value
    return ""


def _detail_bool(payload: dict[str, Any], key: str) -> bool:  # pyright: ignore[reportUnusedFunction] — imported by observability._tree_render
    return payload.get(key) is True


def _child_sort_key(span_id: UUID, spans_by_id: dict[UUID, SpanRecord]) -> tuple[Any, int, int, str]:
    span = spans_by_id[span_id]
    return (
        span.started_at,
        span.sequence_no,
        _SPAN_KIND_PRIORITY.get(span.kind, len(_SPAN_KIND_PRIORITY)),
        str(span.span_id),
    )


def _build_children_map(tree: list[SpanRecord]) -> dict[UUID | None, list[UUID]]:
    children_map: dict[UUID | None, list[UUID]] = {}
    spans_by_id = {span.span_id: span for span in tree}
    for span in tree:
        children_map.setdefault(span.parent_span_id, []).append(span.span_id)
    for parent_id, child_ids in children_map.items():
        children_map[parent_id] = sorted(child_ids, key=lambda child_id: _child_sort_key(child_id, spans_by_id))
    return children_map


def _select_root_span(tree: list[SpanRecord], root_deployment_id: UUID) -> SpanRecord | None:
    deployment_matches = [
        span for span in tree if span.kind == SpanKind.DEPLOYMENT and span.deployment_id == root_deployment_id
    ]
    if deployment_matches:
        return min(deployment_matches, key=lambda span: (span.started_at, span.sequence_no, str(span.span_id)))

    top_level_matches = [span for span in tree if span.kind == SpanKind.DEPLOYMENT and span.parent_span_id is None]
    if top_level_matches:
        return min(top_level_matches, key=lambda span: (span.started_at, span.sequence_no, str(span.span_id)))

    deployment_spans = [span for span in tree if span.kind == SpanKind.DEPLOYMENT]
    if deployment_spans:
        return min(deployment_spans, key=lambda span: (span.started_at, span.sequence_no, str(span.span_id)))
    return None


def _compute_descendant_costs(
    *,
    spans_by_id: dict[UUID, SpanRecord],
    children_map: dict[UUID | None, list[UUID]],
    metrics_by_id: dict[UUID, dict[str, Any]],
) -> tuple[dict[UUID, float], dict[UUID, _TokenTotals]]:
    descendant_costs: dict[UUID, float] = {}
    descendant_tokens: dict[UUID, _TokenTotals] = {}
    visiting: set[UUID] = set()

    def _visit(span_id: UUID) -> tuple[float, _TokenTotals]:
        if span_id in descendant_costs:
            return descendant_costs[span_id], descendant_tokens[span_id]
        if span_id in visiting:
            return 0.0, _TokenTotals()

        visiting.add(span_id)
        total_cost = 0.0
        ti, to, tc, tr = 0, 0, 0, 0
        for child_id in children_map.get(span_id, []):
            child = spans_by_id[child_id]
            total_cost += child.cost_usd
            if child.kind == SpanKind.LLM_ROUND:
                metrics = metrics_by_id[child_id]
                context = f"Span {child_id}"
                ti += _detail_int(metrics, TOKENS_INPUT_KEY, context=context, field_name="metrics_json")
                to += _detail_int(metrics, TOKENS_OUTPUT_KEY, context=context, field_name="metrics_json")
                tc += _detail_int(metrics, TOKENS_CACHE_READ_KEY, context=context, field_name="metrics_json")
                tr += _detail_int(metrics, TOKENS_REASONING_KEY, context=context, field_name="metrics_json")
            child_cost, child_tokens = _visit(child_id)
            total_cost += child_cost
            ti += child_tokens.tokens_input
            to += child_tokens.tokens_output
            tc += child_tokens.tokens_cache_read
            tr += child_tokens.tokens_reasoning
        descendant_costs[span_id] = total_cost
        descendant_tokens[span_id] = _TokenTotals(
            tokens_input=ti, tokens_output=to, tokens_cache_read=tc, tokens_reasoning=tr
        )
        visiting.remove(span_id)
        return total_cost, descendant_tokens[span_id]

    for span_id in spans_by_id:
        _visit(span_id)
    return descendant_costs, descendant_tokens


def _sum_span_totals(tree: list[SpanRecord]) -> CostTotals:
    return aggregate_cost_totals((span.kind, span.cost_usd, span.metrics_json, f"Span {span.span_id}") for span in tree)


def _collect_document_shas(tree: list[SpanRecord]) -> set[str]:
    shas: set[str] = set()
    for span in tree:
        shas.update(span.input_document_shas)
        shas.update(span.output_document_shas)
    return shas


def build_span_tree_view(tree: list[SpanRecord], root_deployment_id: UUID) -> SpanTreeView | None:
    """Build reusable indexes and totals for a span deployment tree."""
    if not tree:
        return None

    root_span = _select_root_span(tree, root_deployment_id)
    if root_span is None:
        return None

    spans_by_id = {span.span_id: span for span in tree}
    meta_by_id = {
        span.span_id: parse_json_object(span.meta_json, context=f"Span {span.span_id}", field_name="meta_json")
        for span in tree
    }
    metrics_by_id = {
        span.span_id: parse_json_object(span.metrics_json, context=f"Span {span.span_id}", field_name="metrics_json")
        for span in tree
    }
    children_map = _build_children_map(tree)
    descendant_costs, descendant_tokens = _compute_descendant_costs(
        spans_by_id=spans_by_id,
        children_map=children_map,
        metrics_by_id=metrics_by_id,
    )
    return SpanTreeView(
        root_span=root_span,
        spans_by_id=spans_by_id,
        children_map=children_map,
        meta_by_id=meta_by_id,
        metrics_by_id=metrics_by_id,
        counts_by_kind=Counter(span.kind for span in tree),
        document_shas=_collect_document_shas(tree),
        descendant_costs=descendant_costs,
        descendant_tokens=descendant_tokens,
        totals=_sum_span_totals(tree),
    )
