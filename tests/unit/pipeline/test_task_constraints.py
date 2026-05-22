# pyright: reportPrivateUsage=false
"""Tests for pipeline constraints: create_root context guards, collect_tasks deadlines/ordering, and PipelineFlow validation."""

import asyncio

import pytest

from ai_pipeline_core.documents import Document
from ai_pipeline_core.pipeline import PipelineFlow, PipelineTask, collect_tasks, pipeline_test_context
from ai_pipeline_core.pipeline.options import FlowOptions


# ---------------------------------------------------------------------------
# Document types
# ---------------------------------------------------------------------------


class LowInputDoc(Document):
    """Input for low-priority tests."""


class LowOutputDoc(Document):
    """Output for low-priority tests."""


def test_create_root_outside_context_succeeds() -> None:
    """Document.create_root() works when no task context is active."""
    doc = LowInputDoc.create_root(name="ok.txt", content="x", reason="no context")
    assert doc.name == "ok.txt"


def test_create_root_inside_test_context_succeeds() -> None:
    """Document.create_root() is allowed inside pipeline_test_context (scope_kind='test')."""
    with pipeline_test_context():
        doc = LowInputDoc.create_root(name="test-root.txt", content="x", reason="test context")
    assert doc.name == "test-root.txt"


# ---------------------------------------------------------------------------
# collect_tasks deadline boundary
# ---------------------------------------------------------------------------


class _SlowTask(PipelineTask):
    @classmethod
    async def run(cls, input_docs: tuple[LowInputDoc, ...], delay: float) -> tuple[LowOutputDoc, ...]:
        await asyncio.sleep(delay)
        return (LowOutputDoc.derive(derived_from=(input_docs[0],), name="slow.txt", content="done"),)


@pytest.mark.asyncio
async def test_collect_tasks_deadline_zero_returns_all_incomplete() -> None:
    """deadline_seconds=0 returns all handles as incomplete (no time to complete)."""
    doc = LowInputDoc.create_root(name="in.txt", content="x", reason="gap23")
    with pipeline_test_context():
        batch = await collect_tasks(
            _SlowTask.run((doc,), delay=1.0),
            _SlowTask.run((doc,), delay=1.0),
            deadline_seconds=0,
        )

    assert batch.completed == []
    assert len(batch.incomplete) == 2


@pytest.mark.asyncio
async def test_collect_tasks_empty_returns_empty_batch() -> None:
    """collect_tasks with no handles returns empty batch."""
    batch = await collect_tasks()
    assert batch.completed == []
    assert batch.incomplete == []


@pytest.mark.asyncio
async def test_collect_tasks_completed_within_deadline() -> None:
    """Fast tasks complete within generous deadline."""
    doc = LowInputDoc.create_root(name="in.txt", content="x", reason="gap23c")
    with pipeline_test_context():
        batch = await collect_tasks(
            _SlowTask.run((doc,), delay=0.01),
            _SlowTask.run((doc,), delay=0.01),
            deadline_seconds=5.0,
        )

    assert len(batch.completed) == 2
    assert batch.incomplete == []


# ---------------------------------------------------------------------------
# collect_tasks result ordering
# ---------------------------------------------------------------------------


class _TimedTask(PipelineTask):
    @classmethod
    async def run(cls, input_docs: tuple[LowInputDoc, ...], delay: float, label: str) -> tuple[LowOutputDoc, ...]:
        await asyncio.sleep(delay)
        return (LowOutputDoc.derive(derived_from=(input_docs[0],), name=f"{label}.txt", content=label),)


@pytest.mark.asyncio
async def test_collect_tasks_returns_in_completion_order() -> None:
    """collect_tasks returns results in completion order (fast first)."""
    doc = LowInputDoc.create_root(name="in.txt", content="x", reason="gap24")
    with pipeline_test_context():
        batch = await collect_tasks(
            _TimedTask.run((doc,), delay=0.15, label="slow"),
            _TimedTask.run((doc,), delay=0.01, label="fast"),
        )

    assert len(batch.completed) == 2
    # Fast completes first, so appears first in completed list
    labels = [docs[0].name for docs in batch.completed]
    assert labels[0] == "fast.txt"
    assert labels[1] == "slow.txt"


# ---------------------------------------------------------------------------
# PipelineFlow.__init_subclass__ edge cases
# ---------------------------------------------------------------------------


def test_flow_rejects_test_prefix_name() -> None:
    """Flow class name starting with 'Test' is rejected."""
    with pytest.raises(TypeError, match="cannot start with 'Test'"):

        class TestBadFlow(PipelineFlow):
            async def run(self, input_docs: tuple[LowInputDoc, ...], options: FlowOptions) -> tuple[LowOutputDoc, ...]:
                _ = (input_docs, options)
                return ()


def test_flow_rejects_sync_run() -> None:
    """Flow with synchronous run() is rejected."""
    with pytest.raises(TypeError, match="must be async def"):

        class SyncFlow(PipelineFlow):
            def run(self, input_docs: tuple[LowInputDoc, ...], options: FlowOptions) -> tuple[LowOutputDoc, ...]:  # type: ignore[override]
                _ = (input_docs, options)
                return ()


def test_flow_rejects_missing_named_input() -> None:
    """Flow run() with no named inputs is rejected."""
    with pytest.raises(TypeError, match="at least one named input parameter"):

        class BadParamFlow(PipelineFlow):
            async def run(self) -> tuple[LowOutputDoc, ...]:  # type: ignore[override]
                return ()


def test_flow_rejects_low_estimated_minutes() -> None:
    """Flow with estimated_minutes < 1 is rejected."""
    with pytest.raises(TypeError, match="estimated_minutes"):

        class FastFlow(PipelineFlow):
            estimated_minutes = 0.5

            async def run(self, input_docs: tuple[LowInputDoc, ...], options: FlowOptions) -> tuple[LowOutputDoc, ...]:
                _ = (input_docs, options)
                return ()
