"""Replay experimentation helpers built on generic span execution."""

import asyncio
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from ai_pipeline_core._llm_core.types import AIModel
from ai_pipeline_core.database._json_helpers import parse_json_object
from ai_pipeline_core.database._protocol import DatabaseReader, DatabaseWriter
from ai_pipeline_core.database._types import SpanKind, SpanRecord
from ai_pipeline_core.llm.tools import Tool
from ai_pipeline_core.pipeline import safe_gather_indexed

from ._execute import _execute_span_internal

__all__ = [
    "ExperimentOverrides",
    "ExperimentResult",
    "OriginalOutput",
    "experiment_batch",
    "experiment_span",
    "find_experiment_span_ids",
]


@dataclass(frozen=True, slots=True)
class TokenSummary:
    tokens_input: int
    tokens_output: int
    tokens_cache_read: int
    tokens_reasoning: int


@dataclass(frozen=True, slots=True)
class OriginalOutput:
    response_text: str | None
    model: str | None
    cost_usd: float | None
    tokens_input: int | None
    tokens_output: int | None
    duration_ms: int | None
    status: str


@dataclass(frozen=True, slots=True)
class ExperimentResult:
    source_span_id: UUID
    replay_run_id: str
    replay_root_span_id: UUID | None
    original: OriginalOutput
    result: Any
    duration_seconds: float
    cost_usd: float
    model_used: str
    tokens: TokenSummary | None
    recording_degraded: bool = False


@dataclass(frozen=True, slots=True)
class ExperimentOverrides:
    model: AIModel | None = None
    force_deployment_id: str | None = None
    model_options: dict[str, Any] | None = None
    tools: Mapping[str, Tool] | None = None
    response_format: type[BaseModel] | None = None


def _extract_original_output(span: SpanRecord) -> OriginalOutput:
    meta = parse_json_object(span.meta_json, context=f"Span {span.span_id}", field_name="meta_json")
    metrics = parse_json_object(span.metrics_json, context=f"Span {span.span_id}", field_name="metrics_json")
    response_text = meta.get("response_content")
    if not isinstance(response_text, str):
        response_text = None
    model = meta.get("model")
    if not isinstance(model, str):
        model = None
    cost_usd = metrics.get("cost_usd")
    if cost_usd is None:
        cost_value: float | None = None
    else:
        cost_value = float(cost_usd)
    return OriginalOutput(
        response_text=response_text,
        model=model,
        cost_usd=cost_value,
        tokens_input=_optional_int(metrics.get("tokens_input")),
        tokens_output=_optional_int(metrics.get("tokens_output")),
        duration_ms=_optional_int(metrics.get("time_taken_ms")),
        status=span.status,
    )


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _tree_token_summary(spans: Sequence[SpanRecord]) -> TokenSummary | None:
    totals = {
        "tokens_input": 0,
        "tokens_output": 0,
        "tokens_cache_read": 0,
        "tokens_reasoning": 0,
    }
    saw_tokens = False
    for span in spans:
        metrics = parse_json_object(span.metrics_json, context=f"Span {span.span_id}", field_name="metrics_json")
        for key in totals:
            value = metrics.get(key)
            if value is None:
                continue
            totals[key] += int(value)
            saw_tokens = True
    if not saw_tokens:
        return None
    return TokenSummary(**totals)


def _tree_cost(spans: Sequence[SpanRecord]) -> float:
    return sum(span.cost_usd for span in spans)


def _root_span_id(spans: Sequence[SpanRecord], run_id: str) -> UUID | None:
    for span in sorted(spans, key=lambda item: (item.started_at, item.sequence_no, str(item.span_id))):
        if span.run_id == run_id and span.parent_span_id is None:
            return span.span_id
    return None


def _model_used(result: Any, original: OriginalOutput, overrides: ExperimentOverrides | None) -> str:
    if hasattr(result, "model"):
        model = result.model
        if isinstance(model, str):
            return model
    if overrides is not None and overrides.model:
        return overrides.model.name
    return original.model or ""


async def experiment_span(
    span_id: UUID,
    *,
    source_db: DatabaseReader,
    sink_db: DatabaseWriter | None = None,
    overrides: ExperimentOverrides | None = None,
) -> ExperimentResult:
    source_span = await source_db.get_span(span_id)
    if source_span is None:
        raise FileNotFoundError(f"Span {span_id} was not found in the source database.")
    original = _extract_original_output(source_span)

    started_at = time.monotonic()
    outcome = await _execute_span_internal(
        span_id,
        source_db=source_db,
        sink_db=sink_db,
        overrides=overrides,
    )
    duration_seconds = time.monotonic() - started_at

    replay_spans: list[SpanRecord] = []
    root_id = outcome.context.root_deployment_id
    if sink_db is not None and isinstance(sink_db, DatabaseReader) and root_id is not None:
        replay_spans = await sink_db.get_deployment_tree(root_id)

    return ExperimentResult(
        source_span_id=span_id,
        replay_run_id=outcome.context.run_id,
        replay_root_span_id=outcome.context.replay_root_span_id or _root_span_id(replay_spans, outcome.context.run_id),
        original=original,
        result=outcome.result,
        duration_seconds=duration_seconds,
        cost_usd=_tree_cost(replay_spans) if replay_spans else 0.0,
        model_used=_model_used(outcome.result, original, overrides),
        tokens=_tree_token_summary(replay_spans),
        recording_degraded=outcome.context.recording_degraded,
    )


async def experiment_batch(
    span_ids: Sequence[UUID],
    source_db: DatabaseReader,
    *,
    overrides: ExperimentOverrides | None = None,
    concurrency: int = 5,
    sink_db: DatabaseWriter | None = None,
) -> list[ExperimentResult]:
    if concurrency < 1:
        raise ValueError("experiment_batch concurrency must be at least 1.")

    semaphore = asyncio.Semaphore(concurrency)

    async def _run_one(span_id: UUID) -> ExperimentResult:
        async with semaphore:
            return await experiment_span(
                span_id,
                source_db=source_db,
                sink_db=sink_db,
                overrides=overrides,
            )

    results = await safe_gather_indexed(
        *(_run_one(span_id) for span_id in span_ids),
        label="experiment_batch",
        raise_if_all_fail=True,
    )
    return [result for result in results if result is not None]


async def find_experiment_span_ids(
    database: DatabaseReader,
    deployment_id: UUID,
    *,
    kind: str | None = None,
    purpose: str | None = None,
    task_class: str | None = None,
) -> list[UUID]:
    spans = await database.get_deployment_tree(deployment_id)
    matched: list[UUID] = []
    for span in sorted(spans, key=lambda item: (item.started_at, item.sequence_no, str(item.span_id))):
        if span.kind == SpanKind.ATTEMPT:
            continue  # structural grouping, not directly replayable
        if kind is not None and span.kind != kind:
            continue
        meta = parse_json_object(span.meta_json, context=f"Span {span.span_id}", field_name="meta_json")
        if purpose is not None and meta.get("purpose") != purpose:
            continue
        if task_class is not None:
            target_task_class = _target_task_class(span.target)
            if target_task_class != task_class:
                continue
        matched.append(span.span_id)
    return matched


def _target_task_class(target: str) -> str | None:
    if not target.startswith("classmethod:"):
        return None
    _, _, remainder = target.partition(":")
    module_name, _, path = remainder.partition(":")
    class_path, _, method_name = path.rpartition(".")
    if method_name != "run" or not class_path:
        return None
    return f"{module_name}:{class_path}"
