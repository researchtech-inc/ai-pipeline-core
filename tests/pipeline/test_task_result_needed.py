"""Prove PipelineTask.run() cannot return non-document metadata.

Tasks needing to communicate metadata (conversations, parsed results)
to the flow have no sanctioned mechanism beyond ClassVar mutable state.
"""

# pyright: reportUnusedClass=false

import pytest

from ai_pipeline_core.documents.document import Document
from ai_pipeline_core.pipeline._task import PipelineTask


class TRInputDoc(Document):
    """Input for TaskResult tests."""


class TROutputDoc(Document):
    """Output for TaskResult tests."""


class TestTaskReturnConstraint:
    """Prove PipelineTask cannot return metadata alongside documents."""

    def test_task_returning_dict_rejected_at_definition_time(self) -> None:
        """A task annotated to return dict is rejected."""
        with pytest.raises(TypeError, match="must return"):

            class BadReturnTask(PipelineTask):
                @classmethod
                async def run(cls, input_docs: tuple[TRInputDoc, ...]) -> dict:  # type: ignore[override]
                    _ = (cls, input_docs)
                    return {"metadata": "value"}

    def test_task_returning_basemodel_rejected(self) -> None:
        """A task annotated to return a BaseModel is rejected."""
        from pydantic import BaseModel

        class TaskMeta(BaseModel, frozen=True):
            cost: float

        with pytest.raises(TypeError, match="must return"):

            class MetaReturnTask(PipelineTask):
                @classmethod
                async def run(cls, input_docs: tuple[TRInputDoc, ...]) -> TaskMeta:  # type: ignore[override]
                    _ = (cls, input_docs)
                    return TaskMeta(cost=0.5)


class TestTaskResultNotImplemented:
    """Verify TaskResult[T] after implementation."""

    @pytest.mark.xfail(reason="TaskResult[T] not yet implemented", strict=True)
    def test_task_result_importable(self) -> None:
        """TaskResult should be importable from the pipeline module."""
        from ai_pipeline_core.pipeline._task_result import TaskResult  # type: ignore[import-not-found]

        assert TaskResult is not None
