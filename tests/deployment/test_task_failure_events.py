# pyright: reportPrivateUsage=false
"""Tests for TaskFailedEvent emission when a task raises."""

import pytest

from ai_pipeline_core.deployment._types import TaskFailedEvent, _MemoryPublisher
from ai_pipeline_core.documents import Document
from ai_pipeline_core.pipeline import PipelineTask, pipeline_test_context
from ai_pipeline_core.pipeline._execution_context import FlowFrame, set_execution_context


class _FailInputDoc(Document):
    pass


class _FailOutputDoc(Document):
    pass


class _AlwaysFailTask(PipelineTask):
    retries = 0

    @classmethod
    async def run(cls, input_docs: tuple[_FailInputDoc, ...]) -> tuple[_FailOutputDoc, ...]:
        _ = input_docs
        raise ValueError("intentional task failure")


# ---------------------------------------------------------------------------
# Task failure event emission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_failure_still_emits_failed_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """When a task raises, TaskFailedEvent is emitted with the error message.

    Verifies that the error propagation path works — the replay capture
    happens after success, but the failed event must be emitted.
    """
    publisher = _MemoryPublisher()
    doc = _FailInputDoc.create_root(name="in.txt", content="x", reason="gap19")

    flow_frame = FlowFrame(
        name="test-flow",
        flow_class_name="FailTestFlow",
        step=1,
        total_steps=1,
        flow_minutes=(1.0,),
        completed_minutes=0.0,
        flow_params={},
    )

    with pipeline_test_context(publisher=publisher) as ctx:
        with set_execution_context(ctx.with_flow(flow_frame)):
            with pytest.raises(ValueError, match="intentional task failure"):
                await _AlwaysFailTask.run((doc,))

    failed_events = [e for e in publisher.events if isinstance(e, TaskFailedEvent)]
    assert len(failed_events) == 1
    assert "intentional task failure" in failed_events[0].error_message
