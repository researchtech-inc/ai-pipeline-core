"""Tests for task return discipline."""

import pytest

from ai_pipeline_core import Document, PipelineTask, pipeline_test_context


class _RDInput(Document):
    """Input for return-discipline tests."""


class _RDOutput(Document):
    """Output for return-discipline tests."""


class _PassthroughTask(PipelineTask):
    """Returns the input unchanged."""

    @classmethod
    async def run(cls, doc: tuple[_RDInput, ...]) -> tuple[_RDInput, ...]:
        _ = cls
        return doc


class _DeriveTask(PipelineTask):
    """Derives new documents from the input."""

    @classmethod
    async def run(cls, doc: tuple[_RDInput, ...]) -> tuple[_RDOutput, ...]:
        _ = cls
        return tuple(_RDOutput.derive(derived_from=(item,), name=f"out-{item.name}", content=item.text) for item in doc)


class _NoneTask(PipelineTask):
    """Returns no documents."""

    @classmethod
    async def run(cls, doc: tuple[_RDInput, ...]) -> None:
        _ = (cls, doc)


@pytest.mark.asyncio
async def test_returning_input_document_raises() -> None:
    source = _RDInput.create_root(name="test.txt", content="hello", reason="test")
    with pipeline_test_context():
        with pytest.raises(TypeError, match="returned input document\\(s\\) unchanged"):
            await _PassthroughTask.run((source,))


@pytest.mark.asyncio
async def test_derived_document_allowed() -> None:
    source = _RDInput.create_root(name="test.txt", content="hello", reason="test")
    with pipeline_test_context():
        result = await _DeriveTask.run((source,))
    assert len(result) == 1


@pytest.mark.asyncio
async def test_none_return_allowed() -> None:
    source = _RDInput.create_root(name="test.txt", content="hello", reason="test")
    with pipeline_test_context():
        result = await _NoneTask.run((source,))
    assert result == ()
