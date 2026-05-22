"""Shared fixtures for deployment tests."""

import pytest

from ai_pipeline_core import DeploymentResult, Document, FlowOptions
from ai_pipeline_core.pipeline import PipelineFlow, PipelineTask


class InputDoc(Document):
    pass


class MiddleDoc(Document):
    pass


class OutputDoc(Document):
    pass


class _TestOptions(FlowOptions):
    value: str = "ok"


class ToMiddleTask(PipelineTask):
    @classmethod
    async def run(cls, input_docs: tuple[InputDoc, ...]) -> tuple[MiddleDoc, ...]:
        return (MiddleDoc.derive(derived_from=(input_docs[0],), name="middle.txt", content="m"),)


class ToOutputTask(PipelineTask):
    @classmethod
    async def run(cls, middle_docs: tuple[MiddleDoc, ...]) -> tuple[OutputDoc, ...]:
        return (OutputDoc.derive(derived_from=(middle_docs[0],), name="output.txt", content="o"),)


class StageOne(PipelineFlow):
    async def run(self, input_docs: tuple[InputDoc, ...], options: _TestOptions) -> tuple[MiddleDoc, ...]:
        _ = options
        return await ToMiddleTask.run(input_docs=input_docs)


class StageTwo(PipelineFlow):
    async def run(self, middle_docs: tuple[MiddleDoc, ...], options: _TestOptions) -> tuple[OutputDoc, ...]:
        _ = options
        return await ToOutputTask.run(middle_docs=middle_docs)


class _TestResult(DeploymentResult):
    output_count: int = 0


@pytest.fixture
def input_documents() -> list[Document]:
    return [InputDoc.create_root(name="input.txt", content="in", reason="deployment test input")]
