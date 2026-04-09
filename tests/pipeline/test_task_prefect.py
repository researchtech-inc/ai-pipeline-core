# pyright: reportPrivateUsage=false
"""Tests for Prefect integration in PipelineTask and PipelineFlow.

Verifies that:
1. PipelineTask delegates to _prefect_task_fn when FlowRunContext is active
2. PipelineTask falls back to custom retry loop outside FlowRunContext
3. Inherited tasks with overridden ClassVars get correctly configured Prefect tasks
4. PipelineFlow gets a Prefect flow object at class definition time

Tests simulate Prefect flow context via FlowRunContext.model_construct() —
no Prefect API server required, safe for CI with pytest-xdist.
"""

from types import MappingProxyType
from unittest.mock import AsyncMock, patch
from uuid import uuid7

import pytest
from prefect.context import FlowRunContext, TaskRunContext

from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.deployment._types import _NoopPublisher
from ai_pipeline_core.documents import Document
from ai_pipeline_core.pipeline import PipelineTask, pipeline_test_context
from ai_pipeline_core.pipeline._execution_context import (
    ExecutionContext,
    FlowFrame,
    set_execution_context,
)
from ai_pipeline_core.pipeline._runtime_sinks import build_runtime_sinks
from ai_pipeline_core.pipeline.limits import _SharedStatus
from ai_pipeline_core.settings import settings


class _PrefectInputDoc(Document):
    """Input for Prefect integration tests."""


class _PrefectOutputDoc(Document):
    """Output for Prefect integration tests."""


def _make_input() -> _PrefectInputDoc:
    return _PrefectInputDoc.create_root(name="in.txt", content="x", reason="prefect-test")


def _make_execution_context(database: _MemoryDatabase) -> ExecutionContext:
    deployment_id = uuid7()
    flow_span_id = uuid7()
    return ExecutionContext(
        run_id="prefect-test-run",
        execution_id=None,
        publisher=_NoopPublisher(),
        limits=MappingProxyType({}),
        limits_status=_SharedStatus(),
        database=database,
        sinks=build_runtime_sinks(database=database, settings_obj=settings).span_sinks,
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        deployment_name="prefect-test-pipeline",
        flow_frame=FlowFrame(
            name="test-flow",
            flow_class_name="PrefectTestFlow",
            step=1,
            total_steps=1,
            flow_minutes=(1.0,),
            completed_minutes=0.0,
            flow_params={},
        ),
        span_id=flow_span_id,
        current_span_id=flow_span_id,
        flow_span_id=flow_span_id,
    )


# ── Task definitions ──────────────────────────────────────────────────────


class _ProbeTask(PipelineTask):
    """Task that captures whether it ran inside a Prefect TaskRunContext."""

    captured_task_ctx: list[TaskRunContext | None] = []

    @classmethod
    async def run(cls, input_docs: tuple[_PrefectInputDoc, ...]) -> tuple[_PrefectOutputDoc, ...]:
        cls.captured_task_ctx.append(TaskRunContext.get())
        return (_PrefectOutputDoc.derive(derived_from=(input_docs[0],), name="out.txt", content="ok"),)


class _BaseRetryTask(PipelineTask):
    """Base task with no retries."""

    retries = 0
    retry_delay_seconds = 0
    call_count = 0

    @classmethod
    async def run(cls, input_docs: tuple[_PrefectInputDoc, ...]) -> tuple[_PrefectOutputDoc, ...]:
        cls.call_count += 1
        if cls.call_count <= 2:
            raise ValueError("transient failure")
        return (_PrefectOutputDoc.derive(derived_from=(input_docs[0],), name="out.txt", content="ok"),)


class _InheritedRetryTask(_BaseRetryTask):
    """Inherits run() from _BaseRetryTask but overrides retries to 3."""

    retries = 3


class _DirectRetryTask(PipelineTask):
    """Task with retries=2, fails once then succeeds."""

    retries = 2
    retry_delay_seconds = 0
    call_count = 0

    @classmethod
    async def run(cls, input_docs: tuple[_PrefectInputDoc, ...]) -> tuple[_PrefectOutputDoc, ...]:
        cls.call_count += 1
        if cls.call_count == 1:
            raise ValueError("transient failure")
        return (_PrefectOutputDoc.derive(derived_from=(input_docs[0],), name="retried.txt", content="ok"),)


# ── Unit tests: Prefect object configuration ─────────────────────────────


class TestPrefectTaskConfig:
    """Verify Prefect task objects are correctly built at class definition time."""

    def test_prefect_task_fn_created(self) -> None:
        assert _ProbeTask._prefect_task_fn is not None

    def test_prefect_task_has_correct_name(self) -> None:
        assert _ProbeTask._prefect_task_fn.name == "_ProbeTask"

    def test_prefect_task_has_no_retries_by_default(self) -> None:
        assert _ProbeTask._prefect_task_fn.retries == 0

    def test_prefect_task_persist_result_disabled(self) -> None:
        assert _ProbeTask._prefect_task_fn.persist_result is False

    def test_inherited_task_has_own_prefect_task(self) -> None:
        assert _InheritedRetryTask._prefect_task_fn is not _BaseRetryTask._prefect_task_fn

    def test_prefect_task_always_has_zero_retries(self) -> None:
        """Framework owns retries, Prefect decorator always gets retries=0."""
        assert _BaseRetryTask._prefect_task_fn.retries == 0
        assert _InheritedRetryTask._prefect_task_fn.retries == 0

    def test_inherited_task_prefect_name_matches_subclass(self) -> None:
        assert _InheritedRetryTask._prefect_task_fn.name == "_InheritedRetryTask"

    def test_direct_retry_task_prefect_config(self) -> None:
        """Framework owns retries, Prefect decorator always gets retries=0."""
        assert _DirectRetryTask._prefect_task_fn.retries == 0
        assert _DirectRetryTask._prefect_task_fn.retry_delay_seconds == 0


class TestPrefectFlowConfig:
    """Verify PipelineFlow gets a Prefect flow object at class definition time."""

    def test_prefect_flow_fn_created(self) -> None:
        from ai_pipeline_core.pipeline._flow import PipelineFlow
        from ai_pipeline_core.pipeline.options import FlowOptions

        class _ConfigProbeFlow(PipelineFlow):
            async def run(self, input_docs: tuple[_PrefectInputDoc, ...], options: FlowOptions) -> tuple[_PrefectOutputDoc, ...]:
                _ = options
                return tuple(_PrefectOutputDoc.derive(derived_from=(document,), name=f"out-{document.name}", content="ok") for document in input_docs)

        assert _ConfigProbeFlow._prefect_flow_fn is not None
        assert _ConfigProbeFlow._prefect_flow_fn.name == "_ConfigProbeFlow"
        assert _ConfigProbeFlow._prefect_flow_fn.should_validate_parameters is False


# ── Prefect dispatch tests (simulated FlowRunContext, no API server) ───────


def _fake_flow_run_context() -> FlowRunContext:
    """Build a minimal FlowRunContext without connecting to a Prefect server."""
    return FlowRunContext.model_construct()


@pytest.mark.asyncio
async def test_task_delegates_to_prefect_task_fn_inside_flow_context() -> None:
    """When FlowRunContext is active, PipelineTask delegates to _prefect_task_fn."""
    expected_docs = (_PrefectOutputDoc.derive(derived_from=(_make_input(),), name="out.txt", content="ok"),)
    mock_prefect_fn = AsyncMock(return_value=expected_docs)
    db = _MemoryDatabase()

    with (
        FlowRunContext.model_validate(_fake_flow_run_context()),
        patch.object(_ProbeTask, "_prefect_task_fn", mock_prefect_fn),
        set_execution_context(_make_execution_context(db)),
    ):
        result = await _ProbeTask.run((_make_input(),))

    mock_prefect_fn.assert_awaited_once()
    assert result[0].name == "out.txt"


@pytest.mark.asyncio
async def test_task_uses_custom_retries_when_no_flow_context() -> None:
    """Without FlowRunContext, PipelineTask uses _run_with_retries (not _prefect_task_fn)."""
    mock_prefect_fn = AsyncMock()

    with pipeline_test_context(), patch.object(_ProbeTask, "_prefect_task_fn", mock_prefect_fn):
        await _ProbeTask.run((_make_input(),))

    mock_prefect_fn.assert_not_awaited()


@pytest.mark.asyncio
async def test_inherited_task_delegates_with_own_prefect_task_fn() -> None:
    """Subclass that inherits run() but overrides retries uses its own _prefect_task_fn."""
    expected_docs = (_PrefectOutputDoc.derive(derived_from=(_make_input(),), name="out.txt", content="ok"),)
    mock_base_fn = AsyncMock()
    mock_inherited_fn = AsyncMock(return_value=expected_docs)
    db = _MemoryDatabase()

    with (
        FlowRunContext.model_validate(_fake_flow_run_context()),
        patch.object(_BaseRetryTask, "_prefect_task_fn", mock_base_fn),
        patch.object(_InheritedRetryTask, "_prefect_task_fn", mock_inherited_fn),
        set_execution_context(_make_execution_context(db)),
    ):
        result = await _InheritedRetryTask.run((_make_input(),))

    mock_base_fn.assert_not_awaited()
    mock_inherited_fn.assert_awaited_once()
    assert result[0].name == "out.txt"
