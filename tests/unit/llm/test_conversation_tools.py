"""Tests for tool-related paths in Conversation: message conversion, token count, and replay round-trip."""

from typing import Any

import pytest
from pydantic import BaseModel, Field

from ai_pipeline_core._llm_core.model_response import ModelResponse
from ai_pipeline_core._llm_core.types import CoreMessage, Role
from ai_pipeline_core.llm._conversation_runtime import collect_text, to_core_messages
from ai_pipeline_core.llm._engine import InteractionRequest, ToolRuntime, execute_interaction
from ai_pipeline_core.llm.conversation import Conversation, ToolResultMessage
from ai_pipeline_core.llm.tools import Tool, ToolCallRecord, ToolOutput

from tests.support.helpers import make_tool_call

from .conftest import make_response
from tests.support.model_catalog import DEFAULT_TEST_MODEL


# ── Test tools ────────────────────────────────────────────────────────────────


class ToolA(Tool):
    """First tool."""

    class Input(BaseModel):
        x: str = Field(description="Value")

    class Output(BaseModel):
        value: str

    async def run(self, input: Input) -> Output:
        return self.Output(value="a")


class ToolB(Tool):
    """Second tool (name collision with ToolA if we call it tool_a)."""

    class Input(BaseModel):
        y: str = Field(description="Value")

    class Output(BaseModel):
        value: str

    async def run(self, input: Input) -> Output:
        return self.Output(value="b")


# ── to_core_messages ──────────────────────────────────────────────────────────


def test_to_core_messages_tool_result_message() -> None:
    """ToolResultMessage converts to CoreMessage with Role.TOOL and preserves fields."""
    conv = Conversation(model=DEFAULT_TEST_MODEL)
    msg = ToolResultMessage(tool_call_id="c1", function_name="search", content="result text")
    core = to_core_messages((msg,), conv.model)
    assert len(core) == 1
    assert core[0].role == Role.TOOL
    assert core[0].tool_call_id == "c1"
    assert core[0].name == "search"
    assert core[0].content == "result text"


def test_to_core_messages_model_response_with_tool_calls() -> None:
    """ModelResponse with tool_calls converts to assistant CoreMessage with tool_calls."""
    conv = Conversation(model=DEFAULT_TEST_MODEL)
    tc = make_tool_call("c1", "search", '{"q": "test"}')
    resp = make_response(content="Let me search", tool_calls=(tc,))
    core = to_core_messages((resp,), conv.model)
    assert len(core) == 1
    assert core[0].role == Role.ASSISTANT
    assert core[0].content == "Let me search"
    assert core[0].tool_calls is not None
    assert core[0].tool_calls[0].id == "c1"


def test_to_core_messages_model_response_without_tool_calls() -> None:
    """ModelResponse without tool_calls converts to plain assistant CoreMessage."""
    conv = Conversation(model=DEFAULT_TEST_MODEL)
    resp = make_response(content="Final answer")
    core = to_core_messages((resp,), conv.model)
    assert len(core) == 1
    assert core[0].role == Role.ASSISTANT
    assert core[0].tool_calls is None


# ── collect_text ──────────────────────────────────────────────────────────────


def test_collect_text_includes_tool_result() -> None:
    """collect_text includes ToolResultMessage content for substitutor."""
    msg = ToolResultMessage(tool_call_id="c1", function_name="search", content="http://example.com")
    texts = collect_text((msg,))
    assert "http://example.com" in texts


# ── approximate_tokens_count ──────────────────────────────────────────────────


def test_approximate_tokens_count_with_tool_messages() -> None:
    """Token count includes tool result messages."""
    tool_msg = ToolResultMessage(tool_call_id="c1", function_name="search", content="x" * 400)
    resp = make_response(content="y" * 400)
    conv = Conversation(model=DEFAULT_TEST_MODEL, messages=(tool_msg, resp))
    # Each should contribute ~100 tokens (400 chars / 4 chars per token)
    assert conv.approximate_tokens_count >= 200


# ── Duplicate tool name detection ─────────────────────────────────────────────


async def test_duplicate_tool_name_detected() -> None:
    """Two tools with same snake_case name raise ValueError via real send() code path."""
    from ai_pipeline_core.llm.tools import _to_snake_case

    class MySearch(Tool):
        """Search A."""

        class Input(BaseModel):
            q: str = Field(description="Query")

        class Output(BaseModel):
            value: str

        async def run(self, input: Input) -> Output:
            return self.Output(value="a")

    class My_Search(Tool):
        """Search B."""

        class Input(BaseModel):
            q: str = Field(description="Query")

        class Output(BaseModel):
            value: str

        async def run(self, input: Input) -> Output:
            return self.Output(value="b")

    # Verify names collide
    assert _to_snake_case(MySearch.__name__) == _to_snake_case(My_Search.__name__) == "my_search"

    # ValueError is raised before any LLM call, exercising the real _execute_send code path
    conv = Conversation(model=DEFAULT_TEST_MODEL)
    with pytest.raises(ValueError, match="Duplicate tool name"):
        await conv.send("test", tools=[MySearch(), My_Search()])


# ── Bug-proving tests (P0) ───────────────────────────────────────────────────


class DirectAnswer(BaseModel):
    answer: str


async def test_send_structured_with_tools_no_tool_call_single_invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    """When send_structured is called with tools but LLM answers directly (no tool calls),
    only ONE LLM call should be made — not a tool loop call + a second structured call."""

    invocations: list[Any] = []

    async def fake_generate(request: Any) -> ModelResponse[Any]:
        invocations.append(request)
        return make_response(content='{"answer": "42"}')

    monkeypatch.setattr("ai_pipeline_core.llm._engine.generate", fake_generate)

    result = await execute_interaction(
        InteractionRequest(
            messages=(CoreMessage(role=Role.USER, content="test"),),
            model=DEFAULT_TEST_MODEL,
            tools=ToolRuntime(
                schemas=({"type": "function", "function": {"name": "tool_a"}},),
                instances={"tool_a": ToolA()},
                choice="auto",
                max_rounds=5,
            ),
            response_format=DirectAnswer,
            purpose="test",
        )
    )

    _ = result
    # LLM answered directly (no tool calls), so the engine should make exactly 1 generation call.
    assert len(invocations) == 1
    assert result.tool_call_records == ()


async def test_tool_choice_without_tools_raises() -> None:
    """Passing tool_choice without tools should raise ValueError immediately."""
    conv = Conversation(model=DEFAULT_TEST_MODEL)
    with pytest.raises(ValueError, match="tool_choice"):
        await conv.send("test", tool_choice="required")


# ── tool_calls_for ──────────────────────────────────────────────────────────


def _conv_with_records(*records: ToolCallRecord) -> Conversation:
    """Create a Conversation with tool_call_records (Pydantic private fields need model_copy)."""
    return Conversation(model=DEFAULT_TEST_MODEL).model_copy(update={"_tool_call_records": records})


def test_tool_calls_for_filters_by_tool_class() -> None:
    """tool_calls_for returns only records matching the given tool class."""
    conv = _conv_with_records(
        ToolCallRecord(tool=ToolA, input=ToolA.Input(x="1"), output=ToolOutput(content="a1"), round=1),
        ToolCallRecord(tool=ToolB, input=ToolB.Input(y="2"), output=ToolOutput(content="b1"), round=1),
        ToolCallRecord(tool=ToolA, input=ToolA.Input(x="3"), output=ToolOutput(content="a2"), round=2),
    )

    a_records = conv.tool_calls_for(ToolA)
    assert len(a_records) == 2
    assert all(r.tool is ToolA for r in a_records)

    b_records = conv.tool_calls_for(ToolB)
    assert len(b_records) == 1
    assert b_records[0].tool is ToolB


def test_tool_calls_for_empty_when_no_match() -> None:
    """tool_calls_for returns empty tuple when no records match."""
    conv = _conv_with_records(
        ToolCallRecord(tool=ToolA, input=ToolA.Input(x="1"), output=ToolOutput(content="a"), round=1),
    )
    assert conv.tool_calls_for(ToolB) == ()


def test_tool_calls_for_returns_tuple() -> None:
    """tool_calls_for returns immutable tuple, not list."""
    conv = _conv_with_records(
        ToolCallRecord(tool=ToolA, input=ToolA.Input(x="1"), output=ToolOutput(content="a"), round=1),
    )
    result = conv.tool_calls_for(ToolA)
    assert isinstance(result, tuple)


# ── Record accumulation ─────────────────────────────────────────────────────


def test_tool_call_records_not_accumulated_across_sends() -> None:
    """Each send() call produces independent tool_call_records — no cross-send accumulation.

    Phase 1 records must NOT appear in Phase 2's tool_call_records.
    This enables the collection pattern: phase1.tool_call_records + phase2.tool_call_records.
    """
    phase1_records = (ToolCallRecord(tool=ToolA, input=ToolA.Input(x="a"), output=ToolOutput(content="r1"), round=1),)
    conv_after_phase1 = Conversation(model=DEFAULT_TEST_MODEL).model_copy(update={"_tool_call_records": phase1_records})

    phase2_records = (ToolCallRecord(tool=ToolB, input=ToolB.Input(y="b"), output=ToolOutput(content="r2"), round=1),)
    conv_after_phase2 = conv_after_phase1.model_copy(update={"_tool_call_records": phase2_records})

    # Phase 1 has only its own records
    assert len(conv_after_phase1.tool_call_records) == 1
    assert conv_after_phase1.tool_call_records[0].tool is ToolA

    # Phase 2 has only its own records — NOT phase1 + phase2
    assert len(conv_after_phase2.tool_call_records) == 1
    assert conv_after_phase2.tool_call_records[0].tool is ToolB


def test_tool_calls_for_multi_phase_collection() -> None:
    """Collection pattern across phases: no double-counting, no data loss.

    Simulates the primary use case:
        conv = await conv.send_spec(AnalysisSpec(), tools=[inspect])
        critic_conv = await conv.send_spec(CriticSpec(), tools=[inspect])
        all_calls = conv.tool_calls_for(Inspect) + critic_conv.tool_calls_for(Inspect)
    """
    conv_phase1 = _conv_with_records(
        ToolCallRecord(tool=ToolA, input=ToolA.Input(x="p1_1"), output=ToolOutput(content="r1"), round=1),
        ToolCallRecord(tool=ToolA, input=ToolA.Input(x="p1_2"), output=ToolOutput(content="r2"), round=2),
    )
    conv_phase2 = _conv_with_records(
        ToolCallRecord(tool=ToolA, input=ToolA.Input(x="p2_1"), output=ToolOutput(content="r3"), round=1),
    )

    all_calls = conv_phase1.tool_calls_for(ToolA) + conv_phase2.tool_calls_for(ToolA)
    assert len(all_calls) == 3


# ── Phase 7d: Conversation messages ──────────────────────────────────────────


def test_serialize_tool_config_name_and_class_path_only() -> None:
    """_serialize_tool_config returns only name and class_path, no constructor_args."""
    from ai_pipeline_core.llm._conversation_messages import _serialize_tool_config

    config = _serialize_tool_config(ToolA())
    assert set(config.keys()) == {"name", "class_path"}
    assert config["name"] == "tool_a"


def test_serialize_tool_config_no_api_key_leakage() -> None:
    """Tool with API key in __init__ state: _serialize_tool_config excludes it."""
    from ai_pipeline_core.llm._conversation_messages import _serialize_tool_config

    class ApiTool(Tool):
        """Tool with API key."""

        class Input(BaseModel):
            q: str = Field(description="q")

        class Output(BaseModel):
            result: str

        def __init__(self, api_key: str) -> None:
            self._api_key = api_key

        async def run(self, input: Input) -> Output:
            return self.Output(result="ok")

    config = _serialize_tool_config(ApiTool(api_key="secret-123"))
    assert "secret-123" not in str(config)
    assert "constructor_args" not in config
