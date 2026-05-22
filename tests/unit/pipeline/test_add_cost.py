"""Tests for add_cost() public API and BASE_COST_USD class variable."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4, uuid7

import pytest

from ai_pipeline_core import add_cost
from ai_pipeline_core.database import SpanKind, SpanRecord
from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.pipeline._execution_context import (
    ExecutionContext,
    FlowFrame,
    add_cost as add_cost_from_execution_context,
    pipeline_test_context,
    set_execution_context,
)
from ai_pipeline_core.pipeline._span_sink import DatabaseSpanSink
from ai_pipeline_core.pipeline._span_types import SpanContext
from ai_pipeline_core.pipeline._track_span import get_current_span_context, track_span
from ai_pipeline_core.pipeline.limits import _SharedStatus


# --- SpanContext unit tests ---


class TestSpanContextAddCost:
    def test_add_cost_accumulates(self) -> None:
        ctx = SpanContext(span_id=uuid7(), parent_span_id=None)
        ctx._add_cost(0.005)
        ctx._add_cost(0.003)
        assert ctx._added_cost_usd == pytest.approx(0.008)

    def test_build_metrics_includes_added_cost(self) -> None:
        ctx = SpanContext(span_id=uuid7(), parent_span_id=None)
        ctx._add_cost(0.05)
        now = datetime.now(UTC)
        metrics = ctx._build_metrics(ended_at=now, started_at=now - timedelta(seconds=1), log_summary={})
        assert metrics.cost_usd == pytest.approx(0.05)

    def test_build_metrics_merges_llm_and_added_cost(self) -> None:
        ctx = SpanContext(span_id=uuid7(), parent_span_id=None)
        ctx.set_metrics(cost_usd=0.30)
        ctx._add_cost(0.05)
        now = datetime.now(UTC)
        metrics = ctx._build_metrics(ended_at=now, started_at=now - timedelta(seconds=1), log_summary={})
        assert metrics.cost_usd == pytest.approx(0.35)

    def test_build_metrics_cost_none_when_no_cost(self) -> None:
        ctx = SpanContext(span_id=uuid7(), parent_span_id=None)
        now = datetime.now(UTC)
        metrics = ctx._build_metrics(ended_at=now, started_at=now - timedelta(seconds=1), log_summary={})
        assert metrics.cost_usd is None

    def test_build_metrics_preserves_llm_only_cost(self) -> None:
        ctx = SpanContext(span_id=uuid7(), parent_span_id=None)
        ctx.set_metrics(cost_usd=0.10)
        now = datetime.now(UTC)
        metrics = ctx._build_metrics(ended_at=now, started_at=now - timedelta(seconds=1), log_summary={})
        assert metrics.cost_usd == pytest.approx(0.10)


# --- add_cost() free function tests ---


class TestAddCostFunction:
    def test_negative_amount_raises(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            add_cost(-0.01)

    def test_nan_raises(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            add_cost(float("nan"))

    def test_inf_raises(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            add_cost(float("inf"))

    def test_negative_inf_raises(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            add_cost(float("-inf"))

    def test_zero_is_noop(self) -> None:
        add_cost(0.0)

    def test_noop_outside_execution_context(self) -> None:
        add_cost(0.05)

    def test_reason_is_accepted_and_discarded(self) -> None:
        add_cost(0.0, reason="search api")

    def test_exported_from_top_level(self) -> None:
        assert add_cost is add_cost_from_execution_context


# --- ContextVar integration tests ---


class TestAddCostWithSpan:
    @pytest.mark.asyncio
    async def test_add_cost_reaches_span_context(self) -> None:
        with pipeline_test_context():
            async with track_span(SpanKind.OPERATION, "test-op", "", sinks=()) as span_ctx:
                add_cost(0.006)
                add_cost(0.004)
                assert span_ctx._added_cost_usd == pytest.approx(0.01)

    @pytest.mark.asyncio
    async def test_nested_spans_cost_isolation(self) -> None:
        with pipeline_test_context():
            async with track_span(SpanKind.OPERATION, "outer", "", sinks=()) as outer:
                add_cost(0.10)
                async with track_span(SpanKind.OPERATION, "inner", "", sinks=()) as inner:
                    add_cost(0.05)
                    assert inner._added_cost_usd == pytest.approx(0.05)
                assert outer._added_cost_usd == pytest.approx(0.10)

    @pytest.mark.asyncio
    async def test_add_cost_after_span_exits_targets_parent(self) -> None:
        with pipeline_test_context():
            async with track_span(SpanKind.OPERATION, "parent", "", sinks=()) as parent:
                async with track_span(SpanKind.OPERATION, "child", "", sinks=()):
                    pass
                add_cost(0.01)
                assert parent._added_cost_usd == pytest.approx(0.01)

    @pytest.mark.asyncio
    async def test_context_var_none_after_all_spans_exit(self) -> None:
        with pipeline_test_context():
            async with track_span(SpanKind.OPERATION, "op", "", sinks=()):
                assert get_current_span_context() is not None
            assert get_current_span_context() is None


# --- Database cost totals integration ---


def _make_span(*, root_deployment_id: UUID, kind: str, cost_usd: float, metrics_json: str = "", **kwargs: Any) -> SpanRecord:
    defaults: dict[str, Any] = {
        "span_id": uuid4(),
        "parent_span_id": None,
        "deployment_id": root_deployment_id,
        "root_deployment_id": root_deployment_id,
        "run_id": "test-run",
        "kind": kind,
        "name": "test",
        "sequence_no": 0,
        "cost_usd": cost_usd,
        "metrics_json": metrics_json,
    }
    defaults.update(kwargs)
    return SpanRecord(**defaults)


class TestDatabaseCostTotals:
    @pytest.mark.asyncio
    async def test_memory_includes_non_llm_costs(self) -> None:
        db = _MemoryDatabase()
        rid = uuid4()
        await db.insert_span(_make_span(root_deployment_id=rid, kind=SpanKind.LLM_ROUND, cost_usd=0.20))
        await db.insert_span(_make_span(root_deployment_id=rid, kind=SpanKind.TASK, cost_usd=0.10))
        await db.insert_span(_make_span(root_deployment_id=rid, kind=SpanKind.OPERATION, cost_usd=0.05))

        totals = await db.get_deployment_cost_totals(rid)

        assert totals.cost_usd == pytest.approx(0.35)

    @pytest.mark.asyncio
    async def test_memory_tokens_only_from_llm_round(self) -> None:
        db = _MemoryDatabase()
        rid = uuid4()

        llm_metrics = json.dumps({"tokens_input": 100, "tokens_output": 50})
        task_metrics = json.dumps({"tokens_input": 9999, "tokens_output": 9999})
        await db.insert_span(_make_span(root_deployment_id=rid, kind=SpanKind.LLM_ROUND, cost_usd=0.10, metrics_json=llm_metrics))
        await db.insert_span(_make_span(root_deployment_id=rid, kind=SpanKind.TASK, cost_usd=0.05, metrics_json=task_metrics))

        totals = await db.get_deployment_cost_totals(rid)

        assert totals.tokens_input == 100
        assert totals.tokens_output == 50


# --- BASE_COST_USD validation ---


class TestBaseCostUsdTask:
    def test_default_is_zero(self) -> None:
        from ai_pipeline_core.pipeline._task import PipelineTask

        assert PipelineTask.BASE_COST_USD == 0.0

    def test_negative_raises(self) -> None:
        from ai_pipeline_core.pipeline._task import PipelineTask

        with pytest.raises(TypeError, match="BASE_COST_USD"):
            type("BadCostTask", (PipelineTask,), {"name": "BadCostTask", "BASE_COST_USD": -0.5, "estimated_minutes": 1.0})

    def test_inf_raises(self) -> None:
        from ai_pipeline_core.pipeline._task import PipelineTask

        with pytest.raises(TypeError, match="BASE_COST_USD"):
            type("InfCostTask", (PipelineTask,), {"name": "InfCostTask", "BASE_COST_USD": float("inf"), "estimated_minutes": 1.0})

    def test_nan_raises(self) -> None:
        from ai_pipeline_core.pipeline._task import PipelineTask

        with pytest.raises(TypeError, match="BASE_COST_USD"):
            type("NanCostTask", (PipelineTask,), {"name": "NanCostTask", "BASE_COST_USD": float("nan"), "estimated_minutes": 1.0})


class TestBaseCostUsdFlow:
    def test_default_is_zero(self) -> None:
        from ai_pipeline_core.pipeline._flow import PipelineFlow

        assert PipelineFlow.BASE_COST_USD == 0.0

    def test_negative_raises(self) -> None:
        from ai_pipeline_core.pipeline._flow import PipelineFlow

        with pytest.raises(TypeError, match="BASE_COST_USD"):
            type("NegCostFlow", (PipelineFlow,), {"name": "NegCostFlow", "BASE_COST_USD": -1.0, "estimated_minutes": 1.0})

    def test_inf_raises(self) -> None:
        from ai_pipeline_core.pipeline._flow import PipelineFlow

        with pytest.raises(TypeError, match="BASE_COST_USD"):
            type("InfCostFlow", (PipelineFlow,), {"name": "InfCostFlow", "BASE_COST_USD": float("inf"), "estimated_minutes": 1.0})

    def test_nan_raises(self) -> None:
        from ai_pipeline_core.pipeline._flow import PipelineFlow

        with pytest.raises(TypeError, match="BASE_COST_USD"):
            type("NanCostFlow", (PipelineFlow,), {"name": "NanCostFlow", "BASE_COST_USD": float("nan"), "estimated_minutes": 1.0})


# --- End-to-end: add_cost → DatabaseSpanSink → SpanRecord ---


def _make_execution_context(database: _MemoryDatabase) -> ExecutionContext:
    from types import MappingProxyType

    from ai_pipeline_core.deployment._types import _NoopPublisher

    deployment_id = uuid4()
    sink = DatabaseSpanSink(database)
    return ExecutionContext(
        run_id="cost-test-run",
        execution_id=None,
        publisher=_NoopPublisher(),
        limits=MappingProxyType({}),
        limits_status=_SharedStatus(),
        database=database,
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        sinks=(sink,),
    )


class TestAddCostEndToEnd:
    @pytest.mark.asyncio
    async def test_add_cost_persists_to_span_record(self) -> None:
        db = _MemoryDatabase()
        ctx = _make_execution_context(db)
        with set_execution_context(ctx):
            async with track_span(SpanKind.OPERATION, "api-call", "", sinks=ctx.sinks, db=ctx.database) as span_ctx:
                add_cost(0.006)
                add_cost(0.004)
                span_id = span_ctx.span_id

        span = await db.get_span(span_id)
        assert span is not None
        assert span.cost_usd == pytest.approx(0.01)

    @pytest.mark.asyncio
    async def test_add_cost_plus_llm_cost_merged_in_span_record(self) -> None:
        db = _MemoryDatabase()
        ctx = _make_execution_context(db)
        with set_execution_context(ctx):
            async with track_span(SpanKind.OPERATION, "mixed-cost", "", sinks=ctx.sinks, db=ctx.database) as span_ctx:
                span_ctx.set_metrics(cost_usd=0.30)
                add_cost(0.05)
                span_id = span_ctx.span_id

        span = await db.get_span(span_id)
        assert span is not None
        assert span.cost_usd == pytest.approx(0.35)

    @pytest.mark.asyncio
    async def test_cost_totals_include_non_llm_span(self) -> None:
        db = _MemoryDatabase()
        ctx = _make_execution_context(db)
        with set_execution_context(ctx):
            async with track_span(SpanKind.TASK, "task-with-cost", "", sinks=ctx.sinks, db=ctx.database):
                add_cost(0.10)
            async with track_span(SpanKind.OPERATION, "op-with-cost", "", sinks=ctx.sinks, db=ctx.database):
                add_cost(0.05)

        totals = await db.get_deployment_cost_totals(ctx.root_deployment_id)
        assert totals.cost_usd == pytest.approx(0.15)
        assert totals.tokens_input == 0


# --- Real-time cost accumulation on ExecutionContext ---


class TestExecutionContextTotalCost:
    @pytest.mark.asyncio
    async def test_total_cost_accumulates_across_spans(self) -> None:
        db = _MemoryDatabase()
        ctx = _make_execution_context(db)
        with set_execution_context(ctx):
            async with track_span(SpanKind.OPERATION, "op-1", "", sinks=ctx.sinks, db=ctx.database):
                add_cost(0.10)
            assert ctx.total_cost_usd == pytest.approx(0.10)

            async with track_span(SpanKind.OPERATION, "op-2", "", sinks=ctx.sinks, db=ctx.database):
                add_cost(0.05)
            assert ctx.total_cost_usd == pytest.approx(0.15)

    @pytest.mark.asyncio
    async def test_total_cost_includes_llm_set_metrics(self) -> None:
        db = _MemoryDatabase()
        ctx = _make_execution_context(db)
        with set_execution_context(ctx):
            async with track_span(SpanKind.LLM_ROUND, "llm-1", "", sinks=ctx.sinks, db=ctx.database) as span_ctx:
                span_ctx.set_metrics(cost_usd=0.30)
        assert ctx.total_cost_usd == pytest.approx(0.30)

    @pytest.mark.asyncio
    async def test_total_cost_zero_when_no_cost(self) -> None:
        db = _MemoryDatabase()
        ctx = _make_execution_context(db)
        with set_execution_context(ctx):
            async with track_span(SpanKind.OPERATION, "free-op", "", sinks=ctx.sinks, db=ctx.database):
                pass
        assert ctx.total_cost_usd == 0.0

    @pytest.mark.asyncio
    async def test_total_cost_shared_across_derived_contexts(self) -> None:
        db = _MemoryDatabase()
        ctx = _make_execution_context(db)
        derived = ctx.with_flow(FlowFrame(name="f", flow_class_name="F", step=1, total_steps=1, flow_minutes=(1.0,), completed_minutes=0.0, flow_params={}))
        with set_execution_context(derived):
            async with track_span(SpanKind.OPERATION, "in-flow", "", sinks=derived.sinks, db=derived.database):
                add_cost(0.20)
        assert ctx.total_cost_usd == pytest.approx(0.20)
        assert derived.total_cost_usd == pytest.approx(0.20)


# --- Span tree view with non-LLM costs ---


class TestSnapshotCosts:
    def test_summary_total_includes_non_llm_costs(self) -> None:
        from ai_pipeline_core.database.snapshot._spans import build_span_tree_view

        rid = uuid4()
        base = datetime(2026, 3, 11, 12, 0, tzinfo=UTC)
        deployment = _make_span(root_deployment_id=rid, kind=SpanKind.DEPLOYMENT, cost_usd=0.0, started_at=base, ended_at=base + timedelta(seconds=10))
        task = _make_span(
            root_deployment_id=rid,
            kind=SpanKind.TASK,
            cost_usd=0.10,
            parent_span_id=deployment.span_id,
            started_at=base + timedelta(seconds=1),
            ended_at=base + timedelta(seconds=5),
        )

        view = build_span_tree_view([deployment, task], rid)

        assert view is not None
        assert view.totals.cost_usd == pytest.approx(0.10)
