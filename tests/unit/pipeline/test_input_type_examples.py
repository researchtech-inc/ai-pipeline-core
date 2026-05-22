"""AI-doc style example for PipelineTask input type validation.

Demonstrates which input types are accepted and rejected by PipelineTask.run().
"""

import enum

import pytest
from pydantic import BaseModel

from ai_pipeline_core import Document
from ai_pipeline_core.pipeline import PipelineTask


class GoodInput(Document):
    pass


class GoodOutput(Document):
    pass


class Priority(enum.StrEnum):
    HIGH = "high"
    LOW = "low"


class FrozenConfig(BaseModel, frozen=True):
    label: str
    max_items: int = 10


# --- Accepted input types ---


def test_document_list_input() -> None:
    class T(PipelineTask):
        @classmethod
        async def run(cls, input_docs: tuple[GoodInput, ...]) -> tuple[GoodOutput, ...]:
            _ = (cls, input_docs)
            return ()

    assert GoodInput in T.input_document_types


def test_scalar_str_input() -> None:
    class T(PipelineTask):
        @classmethod
        async def run(cls, source: GoodInput, label: str) -> tuple[GoodOutput, ...]:
            _ = (cls, source, label)
            return ()


def test_scalar_int_input() -> None:
    class T(PipelineTask):
        @classmethod
        async def run(cls, source: GoodInput, count: int) -> tuple[GoodOutput, ...]:
            _ = (cls, source, count)
            return ()


def test_scalar_float_input() -> None:
    class T(PipelineTask):
        @classmethod
        async def run(cls, source: GoodInput, threshold: float) -> tuple[GoodOutput, ...]:
            _ = (cls, source, threshold)
            return ()


def test_scalar_bool_input() -> None:
    class T(PipelineTask):
        @classmethod
        async def run(cls, source: GoodInput, verbose: bool) -> tuple[GoodOutput, ...]:
            _ = (cls, source, verbose)
            return ()


def test_enum_input() -> None:
    class T(PipelineTask):
        @classmethod
        async def run(cls, source: GoodInput, priority: Priority) -> tuple[GoodOutput, ...]:
            _ = (cls, source, priority)
            return ()


def test_frozen_basemodel_input() -> None:
    class T(PipelineTask):
        @classmethod
        async def run(cls, source: GoodInput, config: FrozenConfig) -> tuple[GoodOutput, ...]:
            _ = (cls, source, config)
            return ()


# --- Rejected input types ---


def test_rejects_set_container() -> None:
    with pytest.raises(TypeError, match="unsupported input annotation"):

        class T(PipelineTask):
            @classmethod
            async def run(cls, documents: set[GoodInput]) -> tuple[GoodOutput, ...]:
                _ = (cls, documents)
                return ()


def test_rejects_bare_list_annotation() -> None:
    with pytest.raises(TypeError, match="bare 'list'"):

        class T(PipelineTask):
            @classmethod
            async def run(cls, documents: list) -> tuple[GoodOutput, ...]:  # pyright: ignore[reportMissingTypeArgument] - intentional bare list to verify validator
                _ = (cls, documents)
                return ()


def test_rejects_bare_dict_annotation() -> None:
    with pytest.raises(TypeError, match="bare 'dict'"):

        class T(PipelineTask):
            @classmethod
            async def run(cls, config: dict) -> tuple[GoodOutput, ...]:  # pyright: ignore[reportMissingTypeArgument] - intentional bare dict to verify validator
                _ = (cls, config)
                return ()


def test_rejects_mutable_basemodel() -> None:
    class MutableConfig(BaseModel):
        label: str

    with pytest.raises(TypeError, match="unsupported input annotation"):

        class T(PipelineTask):
            @classmethod
            async def run(cls, config: MutableConfig) -> tuple[GoodOutput, ...]:
                _ = (cls, config)
                return ()
