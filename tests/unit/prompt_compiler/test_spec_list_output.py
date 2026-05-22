"""Tests for PromptSpec[list[T]] support."""

import typing

import pytest
from pydantic import BaseModel

from ai_pipeline_core.prompt_compiler.spec import PromptSpec


class TaskRole:
    """Test role."""

    text = "test analyst"


# Make it a proper Role subclass
from ai_pipeline_core.prompt_compiler.components import Role


class SpecRole(Role):
    """Test analyst role."""

    text = "test analyst"


class OutputModel(BaseModel):
    text: str
    score: float


# --- PromptSpec[list[BaseModel]] acceptance ---


class ListOutputSpec(PromptSpec[list[OutputModel]]):
    """Spec with list output."""

    input_documents = ()
    role = SpecRole
    task = "Return a list of outputs."


def test_list_output_spec_output_type() -> None:
    origin = typing.get_origin(ListOutputSpec._output_type)
    assert origin is list


def test_list_output_spec_item_type() -> None:
    args = typing.get_args(ListOutputSpec._output_type)
    assert args[0] is OutputModel


def test_list_output_spec_is_not_str() -> None:
    assert ListOutputSpec._output_type is not str


# --- Rejection of invalid list types ---


def test_spec_rejects_list_str() -> None:
    with pytest.raises(TypeError, match="requires T to be a BaseModel subclass"):

        class BadSpec(PromptSpec[list[str]]):  # type: ignore[type-arg]
            """Doc."""

            input_documents = ()
            role = SpecRole
            task = "do it"


def test_spec_rejects_list_int() -> None:
    with pytest.raises(TypeError, match="requires T to be a BaseModel subclass"):

        class BadSpec2(PromptSpec[list[int]]):  # type: ignore[type-arg]
            """Doc."""

            input_documents = ()
            role = SpecRole
            task = "do it"


def test_spec_rejects_nested_list() -> None:
    with pytest.raises(TypeError):

        class BadSpec3(PromptSpec[list[list[OutputModel]]]):  # type: ignore[type-arg]
            """Doc."""

            input_documents = ()
            role = SpecRole
            task = "do it"


# --- output_structure incompatible with list output ---


def test_spec_rejects_output_structure_with_list_output() -> None:
    with pytest.raises(TypeError, match="output_structure is only allowed when output_type is str"):

        class BadSpec4(PromptSpec[list[OutputModel]]):
            """Doc."""

            input_documents = ()
            role = SpecRole
            task = "do it"
            output_structure = "## Section"
