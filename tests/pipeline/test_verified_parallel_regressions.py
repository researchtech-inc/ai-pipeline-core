"""Regression tests for verified pipeline parallel execution bugs."""

from __future__ import annotations

from ai_pipeline_core.documents import Document
from ai_pipeline_core.pipeline import PipelineTask, pipeline_test_context


class _ParallelInputDoc(Document):
    """Input document for parallel regression tests."""


class _ParallelOutputDoc(Document):
    """Output document for parallel regression tests."""


class _DuplicateTask(PipelineTask):
    """Task that intentionally returns the same document twice."""

    @classmethod
    async def run(
        cls,
    ) -> tuple[_ParallelOutputDoc, ...]:
        doc = _ParallelOutputDoc.create_root(name="same.txt", content="same", reason="test")
        return (doc, doc)


async def test_task_duplicate_outputs_keep_original_tuple_shape() -> None:
    with pipeline_test_context():
        result = await _DuplicateTask.run()

    assert len(result) == 2
    assert result[0].sha256 == result[1].sha256
