"""Tests for ToolAvailability and ToolBinding."""

import pytest
from pydantic import BaseModel, Field

from ai_pipeline_core.llm.tools import Tool
from ai_pipeline_core.prompt_contract import ToolAvailability, ToolBinding


class _SampleTool(Tool):
    """Sample tool."""

    class Input(BaseModel):
        """Sample input."""

        query: str = Field(description="query")

    class Output(BaseModel):
        """Sample output."""

        result: str

    async def run(self, input: Input) -> Output:
        return self.Output(result=input.query)


def test_tool_availability_basic() -> None:
    availability = ToolAvailability(_SampleTool, max_calls=2)
    assert availability.tool is _SampleTool
    assert availability.max_calls == 2


def test_tool_availability_rejects_non_tool() -> None:
    with pytest.raises(TypeError, match="must be a Tool subclass"):
        ToolAvailability(str, max_calls=1)  # type: ignore[arg-type]  # negative test: wrong runtime type


def test_tool_availability_rejects_non_positive_max_calls() -> None:
    with pytest.raises(TypeError, match="positive int"):
        ToolAvailability(_SampleTool, max_calls=0)


def test_tool_availability_rejects_negative_max_calls() -> None:
    with pytest.raises(TypeError, match="positive int"):
        ToolAvailability(_SampleTool, max_calls=-1)


def test_tool_availability_rejects_bool_max_calls() -> None:
    with pytest.raises(TypeError, match="positive int"):
        ToolAvailability(_SampleTool, max_calls=True)  # type: ignore[arg-type]  # negative test: wrong runtime type


def test_tool_availability_is_frozen() -> None:
    from dataclasses import FrozenInstanceError

    availability = ToolAvailability(_SampleTool, max_calls=1)
    with pytest.raises(FrozenInstanceError):
        availability.max_calls = 5  # type: ignore[misc]  # frozen model mutation negative test


def test_tool_binding_with_args() -> None:
    binding = ToolBinding(_SampleTool, args={"endpoint": "https://x"})
    assert binding.tool is _SampleTool
    assert binding.args["endpoint"] == "https://x"


def test_tool_binding_default_args_empty() -> None:
    binding = ToolBinding(_SampleTool)
    assert dict(binding.args) == {}


def test_tool_binding_rejects_non_tool() -> None:
    with pytest.raises(TypeError, match="must be a Tool subclass"):
        ToolBinding(str)  # type: ignore[arg-type]  # negative test: wrong runtime type


def test_tool_binding_rejects_non_mapping_args() -> None:
    with pytest.raises(TypeError, match="Mapping"):
        ToolBinding(_SampleTool, args=["x"])  # type: ignore[arg-type]  # negative test: wrong runtime type
