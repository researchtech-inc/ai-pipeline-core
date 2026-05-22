"""Completion-order helper tests."""

import pytest

from ai_pipeline_core import Document
from ai_pipeline_core.pipeline import PipelineTask, as_task_completed, pipeline_test_context


class InputDoc(Document):
    pass


class OutputDoc(Document):
    pass


class EchoTask(PipelineTask):
    @classmethod
    async def run(cls, input_docs: tuple[InputDoc, ...]) -> tuple[OutputDoc, ...]:
        _ = cls
        source = input_docs[0]
        return (OutputDoc.derive(derived_from=(source,), name=f"out_{source.name}", content=source.content),)


@pytest.mark.asyncio
async def test_as_task_completed_yields_results() -> None:
    first = InputDoc.create_root(name="1.txt", content="a", reason="test input")
    second = InputDoc.create_root(name="2.txt", content="b", reason="test input")

    names: list[str] = []
    with pipeline_test_context():
        async for handle in as_task_completed(EchoTask.run((first,)), EchoTask.run((second,))):
            docs = await handle.result()
            names.extend(doc.name for doc in docs)

    assert set(names) == {"out_1.txt", "out_2.txt"}
