"""Regression tests for replay override handling."""

from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import BaseModel, Field

from ai_pipeline_core.llm import AIModel
from ai_pipeline_core.llm.tools import Tool
from ai_pipeline_core.replay._execute import _apply_overrides, _override_tools_in_recorded_order
from tests.support.model_catalog import DEFAULT_TEST_MODEL, ALTERNATE_TEST_MODEL


# ── Helpers ─────────────────────────────────────────────────────────────────


@dataclass
class FakeOverrides:
    """Minimal stand-in for ExperimentOverrides."""

    model: AIModel | None = None
    model_options: dict[str, Any] | None = None
    response_format: type[BaseModel] | None = None
    tools: dict[str, Tool] | None = None


class SearchTool(Tool):
    """Search tool."""

    class Input(BaseModel):
        query: str = Field(description="Query")

    class Output(BaseModel):
        results: str

    async def run(self, input: Input) -> Output:
        return self.Output(results="results")


class SummarizeTool(Tool):
    """Summarize tool."""

    class Input(BaseModel):
        text: str = Field(description="Text to summarize")

    class Output(BaseModel):
        summary: str

    async def run(self, input: Input) -> Output:
        return self.Output(summary="summary")


class NewTool(Tool):
    """Brand new tool not in recording."""

    class Input(BaseModel):
        data: str = Field(description="Data")

    class Output(BaseModel):
        result: str

    async def run(self, input: Input) -> Output:
        return self.Output(result="new result")


def test_response_format_override_applied_when_key_absent() -> None:
    """When overriding an unstructured call to structured, response_format must be applied
    even if 'response_format' key was not in the original arguments.

    Currently: line 119 requires 'response_format' in normalized_arguments — silently drops override.
    """

    class OutputModel(BaseModel):
        answer: str

    receiver = None
    arguments: dict[str, Any] = {"model": ALTERNATE_TEST_MODEL, "model_options": None}
    # No "response_format" key in arguments — this was an unstructured call

    overrides = FakeOverrides(response_format=OutputModel)
    _, new_args = _apply_overrides(receiver=receiver, arguments=arguments, overrides=overrides)

    assert isinstance(new_args, dict)
    assert new_args.get("response_format") is OutputModel, (
        "response_format override should be applied even when key was absent in original arguments"
    )


def test_override_tools_appends_new_tools_after_matching() -> None:
    """When override tools include both matching and new tools, the new ones
    must be appended — not silently dropped.

    Currently: line 64 'return ordered or list(override_tools.values())'
    fallback only fires when ordered is empty. If any tool matched, new-only
    tools are lost.
    """
    recorded_value = [
        {"name": "search_tool", "class_path": "...", "constructor_args": {}},
    ]
    override_tools = {
        "search_tool": SearchTool(),
        "new_tool": NewTool(),
    }

    result = _override_tools_in_recorded_order(recorded_value, override_tools)

    tool_types = [type(t) for t in result]
    assert SearchTool in tool_types, "Matched tool should be present"
    assert NewTool in tool_types, "New tool should be appended, not dropped"
    assert len(result) == 2


def test_override_tools_preserves_recorded_order_and_appends_new() -> None:
    """Matching tools appear in recorded order; new tools are appended at end."""
    recorded_value = [
        {"name": "summarize_tool", "class_path": "...", "constructor_args": {}},
        {"name": "search_tool", "class_path": "...", "constructor_args": {}},
    ]
    override_tools = {
        "search_tool": SearchTool(),
        "summarize_tool": SummarizeTool(),
        "new_tool": NewTool(),
    }

    result = _override_tools_in_recorded_order(recorded_value, override_tools)

    # SummarizeTool first (recorded order), then SearchTool, then NewTool (appended)
    assert isinstance(result[0], SummarizeTool)
    assert isinstance(result[1], SearchTool)
    assert len(result) == 3, f"Expected 3 tools (2 matched + 1 new), got {len(result)}"


def test_model_override_applies_to_constructor_args_receiver() -> None:
    """When replaying a Task/Flow span, model override must apply to receiver
    constructor args (e.g., receiver['value']['model']), not just Conversation.

    Currently: _apply_overrides only handles isinstance(receiver['value'], Conversation).
    """
    receiver = {
        "mode": "constructor_args",
        "value": {"model": DEFAULT_TEST_MODEL, "model_options": None},
    }
    arguments: dict[str, Any] = {"input_data": "test"}

    overrides = FakeOverrides(model=ALTERNATE_TEST_MODEL)
    new_receiver, _ = _apply_overrides(receiver=receiver, arguments=arguments, overrides=overrides)

    assert isinstance(new_receiver, dict)
    assert new_receiver["value"]["model"] == ALTERNATE_TEST_MODEL, (
        "Model override should apply to constructor_args receiver, not just Conversation"
    )


# ── Phase 7e: Replay tool override enforcement ───────────────────────────────


def test_replay_tool_conversation_without_override_tools_raises() -> None:
    """Replaying recorded conversation that used tools without override_tools raises TypeError."""
    recorded_tools = [{"name": "search_tool", "class_path": "tests.unit.replay.test_bugs_replay:SearchTool"}]
    arguments: dict[str, Any] = {"tools": recorded_tools, "content": "test"}

    with pytest.raises(TypeError, match="override_tools"):
        _apply_overrides(receiver=None, arguments=arguments, overrides=None)


def test_replay_tool_conversation_without_override_tools_with_overrides_raises() -> None:
    """Even with model overrides, missing override_tools raises TypeError."""
    recorded_tools = [{"name": "search_tool", "class_path": "tests.unit.replay.test_bugs_replay:SearchTool"}]
    arguments: dict[str, Any] = {"tools": recorded_tools, "content": "test"}
    overrides = FakeOverrides(model=ALTERNATE_TEST_MODEL)

    with pytest.raises(TypeError, match="override_tools"):
        _apply_overrides(receiver=None, arguments=arguments, overrides=overrides)


def test_replay_tool_conversation_with_override_tools_works() -> None:
    """Replaying recorded conversation with override_tools succeeds."""
    recorded_tools = [{"name": "search_tool", "class_path": "tests.unit.replay.test_bugs_replay:SearchTool"}]
    arguments: dict[str, Any] = {"tools": recorded_tools, "content": "test"}
    overrides = FakeOverrides(tools={"search_tool": SearchTool()})

    _, new_args = _apply_overrides(receiver=None, arguments=arguments, overrides=overrides)
    assert isinstance(new_args["tools"], list)
    assert isinstance(new_args["tools"][0], SearchTool)
