"""Tests for ``Tool.bind(**kwargs)`` and the neutral ``ToolBinding`` home."""

import pytest
from pydantic import BaseModel, Field

from ai_pipeline_core import Tool, ToolBinding


class _Stateful(Tool):
    """A tool that takes constructor args."""

    class Input(BaseModel):
        """Input."""

        text: str = Field(description="t")

    class Output(BaseModel):
        """Output."""

        text: str

    def __init__(self, *, suffix: str = "") -> None:
        self._suffix = suffix

    async def run(self, input: Input) -> Output:
        return self.Output(text=f"{input.text}{self._suffix}")


def test_bind_returns_tool_binding() -> None:
    binding = _Stateful.bind(suffix="!")
    assert isinstance(binding, ToolBinding)


def test_bind_attaches_tool_class() -> None:
    binding = _Stateful.bind(suffix="!")
    assert binding.tool is _Stateful


def test_bind_records_kwargs_as_args_dict() -> None:
    binding = _Stateful.bind(suffix="!!")
    assert binding.args == {"suffix": "!!"}


def test_bind_empty_kwargs() -> None:
    binding = _Stateful.bind()
    assert binding.args == {}


def test_tool_binding_importable_from_llm_module() -> None:
    from ai_pipeline_core.llm._tool_binding import ToolBinding as TB

    assert TB is ToolBinding


def test_tool_binding_importable_from_prompt_contract_package() -> None:
    from ai_pipeline_core.prompt_contract import ToolBinding as PCToolBinding

    assert PCToolBinding is ToolBinding


def test_tool_binding_rejects_non_tool() -> None:
    with pytest.raises(TypeError, match="must be a Tool subclass"):
        ToolBinding(tool=str)  # type: ignore[arg-type]  # negative test: wrong runtime type
