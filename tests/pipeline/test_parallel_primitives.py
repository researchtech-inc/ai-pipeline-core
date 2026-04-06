"""Tests for handle-based parallel execution primitives."""

import asyncio
import logging
from typing import Any

import pytest

from ai_pipeline_core.documents import Document
from ai_pipeline_core.pipeline import PipelineTask, TaskBatch, TaskHandle, as_task_completed, collect_tasks
from ai_pipeline_core.pipeline._execution_context import FlowFrame, pipeline_test_context, set_execution_context


class _PDoc(Document):
    """Parallel test document."""


class _FastTask(PipelineTask):
    @classmethod
    async def run(cls, input_docs: tuple[_PDoc, ...]) -> tuple[_PDoc, ...]:
        _ = input_docs
        return (_PDoc(name="fast.txt", content=b"fast"),)


class _SlowTask(PipelineTask):
    @classmethod
    async def run(cls, input_docs: tuple[_PDoc, ...]) -> tuple[_PDoc, ...]:
        _ = input_docs
        await asyncio.sleep(60)
        return (_PDoc(name="slow.txt", content=b"slow"),)


class _FailTask(PipelineTask):
    retries = 0

    @classmethod
    async def run(cls, input_docs: tuple[_PDoc, ...]) -> tuple[_PDoc, ...]:
        _ = input_docs
        raise ValueError("run fail")


def _make_flow_frame() -> FlowFrame:
    return FlowFrame(
        name="parallel-flow",
        flow_class_name="PFlow",
        step=1,
        total_steps=1,
        flow_minutes=(1.0,),
        completed_minutes=0.0,
        flow_params={},
    )


def _make_doc(content: str = "test") -> _PDoc:
    return _PDoc(name="input.txt", content=content.encode())


@pytest.mark.asyncio
async def test_run_returns_task_handle() -> None:
    with pipeline_test_context() as ctx:
        with set_execution_context(ctx.with_flow(_make_flow_frame())):
            handle: Any = _FastTask.run((_make_doc(),))
            assert isinstance(handle, TaskHandle)
            result = await handle.result()
            assert len(result) == 1


@pytest.mark.asyncio
async def test_task_handle_done_property() -> None:
    with pipeline_test_context() as ctx:
        with set_execution_context(ctx.with_flow(_make_flow_frame())):
            handle = _FastTask.run((_make_doc(),))
            await handle.result()
            assert handle.done is True


@pytest.mark.asyncio
async def test_task_handle_cancel() -> None:
    with pipeline_test_context() as ctx:
        with set_execution_context(ctx.with_flow(_make_flow_frame())):
            handle = _SlowTask.run((_make_doc(),))
            handle.cancel()
            with pytest.raises((asyncio.CancelledError, Exception)):
                await handle.result()


@pytest.mark.asyncio
async def test_collect_tasks_all_complete() -> None:
    with pipeline_test_context() as ctx:
        with set_execution_context(ctx.with_flow(_make_flow_frame())):
            h1 = _FastTask.run((_make_doc("a"),))
            h2 = _FastTask.run((_make_doc("b"),))
            batch = await collect_tasks(h1, h2)

    assert isinstance(batch, TaskBatch)
    assert len(batch.completed) == 2
    assert batch.incomplete == []


@pytest.mark.asyncio
async def test_collect_tasks_with_deadline_splits() -> None:
    with pipeline_test_context() as ctx:
        slow: Any = None
        try:
            with set_execution_context(ctx.with_flow(_make_flow_frame())):
                fast = _FastTask.run((_make_doc(),))
                slow = _SlowTask.run((_make_doc(),))
                batch = await collect_tasks(fast, slow, deadline_seconds=0.5)
        finally:
            if slow is not None:
                slow.cancel()

    assert len(batch.completed) == 1
    assert len(batch.incomplete) == 1


@pytest.mark.asyncio
async def test_collect_tasks_failed_goes_to_incomplete() -> None:
    with pipeline_test_context() as ctx:
        with set_execution_context(ctx.with_flow(_make_flow_frame())):
            good = _FastTask.run((_make_doc(),))
            bad = _FailTask.run((_make_doc(),))
            batch = await collect_tasks(good, bad)

    assert len(batch.completed) == 1
    assert len(batch.incomplete) == 1


@pytest.mark.asyncio
async def test_collect_tasks_accepts_list() -> None:
    with pipeline_test_context() as ctx:
        with set_execution_context(ctx.with_flow(_make_flow_frame())):
            handles = [_FastTask.run((_make_doc("x"),)) for _ in range(3)]
            batch = await collect_tasks(handles)

    assert len(batch.completed) == 3
    assert batch.incomplete == []


@pytest.mark.asyncio
async def test_collect_tasks_warns_on_high_fan_out(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="ai_pipeline_core.pipeline._parallel")
    with pipeline_test_context() as ctx:
        with set_execution_context(ctx.with_flow(_make_flow_frame())):
            handles = [_FastTask.run((_make_doc(str(index)),)) for index in range(51)]
            batch = await collect_tasks(handles)

    assert len(batch.completed) == 51
    assert "exceeds the global warning threshold of 50" in caplog.text
    assert "declare max_fan_out = 51 on the flow class to document it" in caplog.text
    assert "max_fan_out is informational only" in caplog.text


@pytest.mark.asyncio
async def test_as_task_completed_yields_handles() -> None:
    with pipeline_test_context() as ctx:
        with set_execution_context(ctx.with_flow(_make_flow_frame())):
            h1 = _FastTask.run((_make_doc("1"),))
            h2 = _FastTask.run((_make_doc("2"),))
            yielded = [handle async for handle in as_task_completed(h1, h2)]

    assert len(yielded) == 2
    assert all(isinstance(handle, TaskHandle) for handle in yielded)


@pytest.mark.asyncio
async def test_collect_tasks_on_dispatched_handle_list() -> None:
    with pipeline_test_context() as ctx:
        with set_execution_context(ctx.with_flow(_make_flow_frame())):
            handles = [_FastTask.run((_make_doc(label),)) for label in ("a", "b", "c")]
            batch = await collect_tasks(handles)

    assert len(batch.completed) == 3
    assert batch.incomplete == []
