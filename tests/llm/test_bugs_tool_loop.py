"""Regression tests for tool loop forced-final response handling."""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any
from uuid import uuid7

from pydantic import BaseModel, Field

from ai_pipeline_core._llm_core.model_response import ModelResponse
from ai_pipeline_core._llm_core.types import CoreMessage, Role
from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.deployment._types import _NoopPublisher
from ai_pipeline_core.exceptions import LLMError
from ai_pipeline_core.llm._tool_loop import execute_tool_loop
from ai_pipeline_core.llm.tools import Tool
from ai_pipeline_core.pipeline._execution_context import ExecutionContext, set_execution_context
from ai_pipeline_core.pipeline._runtime_sinks import build_runtime_sinks
from ai_pipeline_core.pipeline.limits import _SharedStatus
from ai_pipeline_core.settings import settings

from .conftest import make_response, make_tool_call


class SearchTool(Tool):
    """Search the web."""

    class Input(BaseModel):
        query: str = Field(description="Search query")

    class Output(BaseModel):
        results: str

    async def run(self, input: Input) -> Output:
        return self.Output(results=f"Results for: {input.query}")


@dataclass(frozen=True)
class _FakeToolResultMsg:
    tool_call_id: str
    function_name: str
    content: str


def _build_msg(tid: str, fn: str, content: str) -> _FakeToolResultMsg:
    return _FakeToolResultMsg(tool_call_id=tid, function_name=fn, content=content)


def _make_context(database: _MemoryDatabase) -> ExecutionContext:
    deployment_id = uuid7()
    span_id = uuid7()
    return ExecutionContext(
        run_id="tool-loop-bug-test",
        execution_id=None,
        publisher=_NoopPublisher(),
        limits=MappingProxyType({}),
        limits_status=_SharedStatus(),
        database=database,
        sinks=build_runtime_sinks(database=database, settings_obj=settings).span_sinks,
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        deployment_name="tool-loop-bug-test",
        span_id=span_id,
        current_span_id=span_id,
        flow_span_id=span_id,
    )


TOOL_SCHEMAS = [{"type": "function", "function": {"name": "search_tool", "parameters": {}}}]


async def test_forced_final_omits_tools() -> None:
    """When max_tool_rounds is exhausted, the forced final LLM call must NOT include
    tool schemas or tool_choice. Currently it sends tools=tool_schemas, tool_choice='none'.
    """
    recorded_calls: list[dict[str, Any]] = []
    round_count = 0

    async def mock_invoke(**kwargs: Any) -> ModelResponse[Any]:
        nonlocal round_count
        recorded_calls.append(kwargs)
        round_count += 1
        if round_count == 1:
            return make_response(
                content="",
                tool_calls=(make_tool_call("c1", "search_tool", '{"query": "test"}'),),
            )
        return make_response(content="Final synthesis")

    database = _MemoryDatabase()
    with set_execution_context(_make_context(database)):
        await execute_tool_loop(
            invoke_llm=mock_invoke,
            tool_schemas=TOOL_SCHEMAS,
            tool_lookup={"search_tool": SearchTool()},
            tool_choice="auto",
            max_tool_rounds=1,
            purpose="test",
            expected_cost=None,
            core_messages=[CoreMessage(role=Role.USER, content="search for test")],
            context_count=0,
            effective_options=None,
            substitutor=None,
            build_tool_result_message=_build_msg,
        )

    # The final call (forced final) should have empty tools and no tool_choice
    forced_final_call = recorded_calls[-1]
    assert forced_final_call["tools"] in ([], None), f"Forced final should not send tool schemas, got: {forced_final_call['tools']}"
    assert forced_final_call["tool_choice"] is None, f"Forced final should not send tool_choice, got: {forced_final_call['tool_choice']}"
    assert forced_final_call.get("tool_schemas") in ([], None), (
        f"Forced final tool_schemas for span metadata should be empty, got: {forced_final_call.get('tool_schemas')}"
    )


async def test_forced_final_failure_preserves_tool_records() -> None:
    """When the forced final LLM call fails (e.g., empty response after retries),
    the accumulated tool_call_records from prior rounds must still be returned.
    Currently they are lost because the exception propagates out of execute_tool_loop.
    """
    round_count = 0

    async def mock_invoke(**kwargs: Any) -> ModelResponse[Any]:
        nonlocal round_count
        round_count += 1
        if round_count == 1:
            return make_response(
                content="",
                tool_calls=(make_tool_call("c1", "search_tool", '{"query": "deep research"}'),),
            )
        # Forced final fails
        raise LLMError("Empty response content — model returned no text and no tool calls.")

    database = _MemoryDatabase()
    with set_execution_context(_make_context(database)):
        msgs, response, records = await execute_tool_loop(
            invoke_llm=mock_invoke,
            tool_schemas=TOOL_SCHEMAS,
            tool_lookup={"search_tool": SearchTool()},
            tool_choice="auto",
            max_tool_rounds=1,
            purpose="test",
            expected_cost=None,
            core_messages=[CoreMessage(role=Role.USER, content="research")],
            context_count=0,
            effective_options=None,
            substitutor=None,
            build_tool_result_message=_build_msg,
        )

    # Tool records from prior rounds must be preserved
    assert len(records) >= 1, "Tool call records from successful rounds should be preserved"
    assert records[0].tool is SearchTool
    # Response should exist (synthetic fallback), not raise
    assert response is not None
    assert response.content  # some content, even if synthetic


async def test_forced_final_failure_preserves_accumulated_messages() -> None:
    """Accumulated messages (assistant responses, tool results) from successful
    rounds must survive forced-final failure.
    """
    round_count = 0

    async def mock_invoke(**kwargs: Any) -> ModelResponse[Any]:
        nonlocal round_count
        round_count += 1
        if round_count <= 2:
            return make_response(
                content="",
                tool_calls=(make_tool_call(f"c{round_count}", "search_tool", '{"query": "q"}'),),
            )
        raise LLMError("Forced final failed")

    database = _MemoryDatabase()
    with set_execution_context(_make_context(database)):
        msgs, response, records = await execute_tool_loop(
            invoke_llm=mock_invoke,
            tool_schemas=TOOL_SCHEMAS,
            tool_lookup={"search_tool": SearchTool()},
            tool_choice="auto",
            max_tool_rounds=2,
            purpose="test",
            expected_cost=None,
            core_messages=[CoreMessage(role=Role.USER, content="multi-round")],
            context_count=0,
            effective_options=None,
            substitutor=None,
            build_tool_result_message=_build_msg,
        )

    # 2 rounds x (ModelResponse + ToolResult) + ModelResponse(fallback) = 5
    assert len(msgs) == 5, f"Expected 5 messages (2 tool rounds + fallback), got {len(msgs)}"
    assert len(records) == 2, f"Expected 2 tool records from 2 rounds, got {len(records)}"


# ── Regression: no limit on consecutive unknown-tool rounds ───────────────────


async def test_unknown_tool_rounds_have_limit() -> None:
    """Loop must abort after MAX_CONSECUTIVE_UNKNOWN_TOOL_ROUNDS consecutive all-unknown rounds.

    Pre-fix: 10 rounds all consumed + 1 forced final = 11 LLM calls.
    Post-fix: 3 unknown rounds + 1 forced final = 4 LLM calls.
    """
    round_count = 0

    async def mock_invoke(**kwargs: Any) -> ModelResponse[Any]:
        nonlocal round_count
        round_count += 1
        if round_count <= 10:
            return make_response(content="", tool_calls=(make_tool_call(f"c{round_count}", "ghost_tool", "{}"),))
        return make_response(content="Forced final")

    database = _MemoryDatabase()
    with set_execution_context(_make_context(database)):
        await execute_tool_loop(
            invoke_llm=mock_invoke,
            tool_schemas=TOOL_SCHEMAS,
            tool_lookup={"search_tool": SearchTool()},
            tool_choice="auto",
            max_tool_rounds=10,
            purpose="test",
            expected_cost=None,
            core_messages=[CoreMessage(role=Role.USER, content="test")],
            context_count=0,
            effective_options=None,
            substitutor=None,
            build_tool_result_message=_build_msg,
        )

    assert round_count == 4  # 3 consecutive unknown rounds + 1 forced final


# ── Regression: forced final lacks steering USER message ──────────────────────


async def test_forced_final_has_steering_user_message() -> None:
    """Forced final call must have a USER steering message as last core_message.

    Pre-fix: last message is a TOOL result — model generates tool calls again.
    Post-fix: last message is USER with 'do not call any tools' instruction.
    """
    recorded_calls: list[dict[str, Any]] = []
    round_count = 0

    async def mock_invoke(**kwargs: Any) -> ModelResponse[Any]:
        nonlocal round_count
        recorded_calls.append(kwargs)
        round_count += 1
        if round_count == 1:
            return make_response(content="", tool_calls=(make_tool_call("c1", "search_tool", '{"query": "test"}'),))
        return make_response(content="done")

    database = _MemoryDatabase()
    with set_execution_context(_make_context(database)):
        await execute_tool_loop(
            invoke_llm=mock_invoke,
            tool_schemas=TOOL_SCHEMAS,
            tool_lookup={"search_tool": SearchTool()},
            tool_choice="auto",
            max_tool_rounds=1,
            purpose="test",
            expected_cost=None,
            core_messages=[CoreMessage(role=Role.USER, content="search for test")],
            context_count=0,
            effective_options=None,
            substitutor=None,
            build_tool_result_message=_build_msg,
        )

    forced_final_msgs = recorded_calls[-1]["core_messages"]
    assert forced_final_msgs[-1].role == Role.USER
    assert "do not call" in forced_final_msgs[-1].content.lower()


async def test_forced_final_steering_message_not_in_accumulated() -> None:
    """The steering USER message must NOT appear in accumulated_messages.

    Persisting the steering message in conversation history would suppress valid
    tool calls in follow-up send() calls (confirmed with grok-4.1-fast).
    The message only exists in core_messages for the immediate forced final call.
    """
    from ai_pipeline_core.llm._conversation_messages import UserMessage

    round_count = 0

    async def mock_invoke(**kwargs: Any) -> ModelResponse[Any]:
        nonlocal round_count
        round_count += 1
        if round_count == 1:
            return make_response(content="", tool_calls=(make_tool_call("c1", "search_tool", '{"query": "test"}'),))
        return make_response(content="done")

    database = _MemoryDatabase()
    with set_execution_context(_make_context(database)):
        msgs, _, _ = await execute_tool_loop(
            invoke_llm=mock_invoke,
            tool_schemas=TOOL_SCHEMAS,
            tool_lookup={"search_tool": SearchTool()},
            tool_choice="auto",
            max_tool_rounds=1,
            purpose="test",
            expected_cost=None,
            core_messages=[CoreMessage(role=Role.USER, content="search for test")],
            context_count=0,
            effective_options=None,
            substitutor=None,
            build_tool_result_message=_build_msg,
        )

    steering_msgs = [m for m in msgs if isinstance(m, UserMessage)]
    assert len(steering_msgs) == 0, "Steering message must not appear in accumulated_messages"


# ── Unknown-tool tracking tests ───────────────────────────────────────────────


async def test_unknown_tool_counter_resets_on_valid_call() -> None:
    """A valid tool call between unknown rounds resets the consecutive counter.

    Pattern: 2 unknown → 1 valid → 2 unknown → natural end.
    Counter never reaches 3 so loop runs to completion without early abort.
    """
    round_count = 0

    async def mock_invoke(**kwargs: Any) -> ModelResponse[Any]:
        nonlocal round_count
        round_count += 1
        if round_count in (1, 2):
            return make_response(content="", tool_calls=(make_tool_call(f"c{round_count}", "ghost", "{}"),))
        if round_count == 3:
            return make_response(content="", tool_calls=(make_tool_call("c3", "search_tool", '{"query": "x"}'),))
        if round_count in (4, 5):
            return make_response(content="", tool_calls=(make_tool_call(f"c{round_count}", "ghost", "{}"),))
        return make_response(content="Done")

    database = _MemoryDatabase()
    with set_execution_context(_make_context(database)):
        _, response, records = await execute_tool_loop(
            invoke_llm=mock_invoke,
            tool_schemas=TOOL_SCHEMAS,
            tool_lookup={"search_tool": SearchTool()},
            tool_choice="auto",
            max_tool_rounds=10,
            purpose="test",
            expected_cost=None,
            core_messages=[CoreMessage(role=Role.USER, content="test")],
            context_count=0,
            effective_options=None,
            substitutor=None,
            build_tool_result_message=_build_msg,
        )

    # Counter: 1, 2, reset→0, 1, 2, text exit — never hit 3
    assert round_count == 6
    assert len(records) == 1  # only the valid search_tool call
    assert response.content == "Done"


async def test_unknown_tool_mixed_with_valid_in_same_round_resets_counter() -> None:
    """Round containing both unknown and valid tool calls is NOT counted as unknown-only.

    The counter resets because at least one call targeted a known tool.
    """
    round_count = 0

    async def mock_invoke(**kwargs: Any) -> ModelResponse[Any]:
        nonlocal round_count
        round_count += 1
        if round_count == 1:
            return make_response(
                content="",
                tool_calls=(
                    make_tool_call("c1a", "ghost", "{}"),
                    make_tool_call("c1b", "search_tool", '{"query": "test"}'),
                ),
            )
        return make_response(content="Final")

    database = _MemoryDatabase()
    with set_execution_context(_make_context(database)):
        msgs, response, records = await execute_tool_loop(
            invoke_llm=mock_invoke,
            tool_schemas=TOOL_SCHEMAS,
            tool_lookup={"search_tool": SearchTool()},
            tool_choice="auto",
            max_tool_rounds=10,
            purpose="test",
            expected_cost=None,
            core_messages=[CoreMessage(role=Role.USER, content="test")],
            context_count=0,
            effective_options=None,
            substitutor=None,
            build_tool_result_message=_build_msg,
        )

    assert round_count == 2  # 1 mixed round + natural finish
    assert len(records) == 1  # only the valid search_tool call


async def test_unknown_tool_single_round_does_not_abort() -> None:
    """One unknown round (counter=1) does not trigger early abort (threshold is 3)."""
    round_count = 0

    async def mock_invoke(**kwargs: Any) -> ModelResponse[Any]:
        nonlocal round_count
        round_count += 1
        if round_count == 1:
            return make_response(content="", tool_calls=(make_tool_call("c1", "ghost", "{}"),))
        return make_response(content="Final")

    database = _MemoryDatabase()
    with set_execution_context(_make_context(database)):
        _, response, _ = await execute_tool_loop(
            invoke_llm=mock_invoke,
            tool_schemas=TOOL_SCHEMAS,
            tool_lookup={"search_tool": SearchTool()},
            tool_choice="auto",
            max_tool_rounds=10,
            purpose="test",
            expected_cost=None,
            core_messages=[CoreMessage(role=Role.USER, content="test")],
            context_count=0,
            effective_options=None,
            substitutor=None,
            build_tool_result_message=_build_msg,
        )

    assert round_count == 2
    assert response.content == "Final"


async def test_unknown_tool_abort_uses_unknown_tools_steering_message() -> None:
    """When loop aborts due to unknown-tool limit, the steering message in
    core_messages mentions that tools did not exist (not the max-rounds wording)."""
    from ai_pipeline_core.llm._tool_loop import _FORCED_FINAL_UNKNOWN_TOOLS_MSG

    recorded_calls: list[dict[str, Any]] = []
    round_count = 0

    async def mock_invoke(**kwargs: Any) -> ModelResponse[Any]:
        nonlocal round_count
        recorded_calls.append(kwargs)
        round_count += 1
        if round_count <= 3:
            return make_response(content="", tool_calls=(make_tool_call(f"c{round_count}", "ghost", "{}"),))
        return make_response(content="Final after abort")

    database = _MemoryDatabase()
    with set_execution_context(_make_context(database)):
        _, response, _ = await execute_tool_loop(
            invoke_llm=mock_invoke,
            tool_schemas=TOOL_SCHEMAS,
            tool_lookup={"search_tool": SearchTool()},
            tool_choice="auto",
            max_tool_rounds=10,
            purpose="test",
            expected_cost=None,
            core_messages=[CoreMessage(role=Role.USER, content="test")],
            context_count=0,
            effective_options=None,
            substitutor=None,
            build_tool_result_message=_build_msg,
        )

    assert round_count == 4  # 3 unknown + 1 forced final
    forced_final_msgs = recorded_calls[-1]["core_messages"]
    assert forced_final_msgs[-1].role == Role.USER
    assert forced_final_msgs[-1].content == _FORCED_FINAL_UNKNOWN_TOOLS_MSG
    assert response.content == "Final after abort"


async def test_max_rounds_exhausted_uses_max_rounds_steering_message() -> None:
    """When loop exits due to max_tool_rounds (not unknown-tool abort),
    the steering message in core_messages uses the max-rounds variant."""
    from ai_pipeline_core.llm._tool_loop import _FORCED_FINAL_MAX_ROUNDS_MSG

    recorded_calls: list[dict[str, Any]] = []
    round_count = 0

    async def mock_invoke(**kwargs: Any) -> ModelResponse[Any]:
        nonlocal round_count
        recorded_calls.append(kwargs)
        round_count += 1
        if round_count <= 2:
            return make_response(content="", tool_calls=(make_tool_call(f"c{round_count}", "search_tool", '{"query": "q"}'),))
        return make_response(content="Final synthesis")

    database = _MemoryDatabase()
    with set_execution_context(_make_context(database)):
        _, response, _ = await execute_tool_loop(
            invoke_llm=mock_invoke,
            tool_schemas=TOOL_SCHEMAS,
            tool_lookup={"search_tool": SearchTool()},
            tool_choice="auto",
            max_tool_rounds=2,
            purpose="test",
            expected_cost=None,
            core_messages=[CoreMessage(role=Role.USER, content="test")],
            context_count=0,
            effective_options=None,
            substitutor=None,
            build_tool_result_message=_build_msg,
        )

    assert round_count == 3  # 2 valid rounds + 1 forced final
    forced_final_msgs = recorded_calls[-1]["core_messages"]
    assert forced_final_msgs[-1].role == Role.USER
    assert forced_final_msgs[-1].content == _FORCED_FINAL_MAX_ROUNDS_MSG
    assert response.content == "Final synthesis"


async def test_max_rounds_1_with_unknown_tool_fires_forced_final_normally() -> None:
    """max_tool_rounds=1 with an unknown-tool first round: counter=1 < 3.

    The loop exits naturally (only 1 iteration), forced final fires via the
    normal max-rounds path, not the unknown-tool-abort path.
    """
    from ai_pipeline_core.llm._tool_loop import _FORCED_FINAL_MAX_ROUNDS_MSG

    recorded_calls: list[dict[str, Any]] = []
    round_count = 0

    async def mock_invoke(**kwargs: Any) -> ModelResponse[Any]:
        nonlocal round_count
        recorded_calls.append(kwargs)
        round_count += 1
        if round_count == 1:
            return make_response(content="", tool_calls=(make_tool_call("c1", "ghost", "{}"),))
        return make_response(content="Forced final")

    database = _MemoryDatabase()
    with set_execution_context(_make_context(database)):
        _, response, _ = await execute_tool_loop(
            invoke_llm=mock_invoke,
            tool_schemas=TOOL_SCHEMAS,
            tool_lookup={"search_tool": SearchTool()},
            tool_choice="auto",
            max_tool_rounds=1,
            purpose="test",
            expected_cost=None,
            core_messages=[CoreMessage(role=Role.USER, content="test")],
            context_count=0,
            effective_options=None,
            substitutor=None,
            build_tool_result_message=_build_msg,
        )

    assert round_count == 2  # 1 unknown round + 1 forced final
    assert response.content == "Forced final"
    # Uses max-rounds message since counter (1) < MAX_CONSECUTIVE_UNKNOWN_TOOL_ROUNDS (3)
    forced_final_msgs = recorded_calls[-1]["core_messages"]
    assert forced_final_msgs[-1].role == Role.USER
    assert forced_final_msgs[-1].content == _FORCED_FINAL_MAX_ROUNDS_MSG


async def test_unknown_tool_abort_steering_injected_before_forced_final_call() -> None:
    """Verify message ordering in core_messages for the forced final call after
    unknown-tool abort: last message must be the USER steering, second-to-last
    must be the TOOL error result from the last unknown-tool round."""
    recorded_calls: list[dict[str, Any]] = []
    round_count = 0

    async def mock_invoke(**kwargs: Any) -> ModelResponse[Any]:
        nonlocal round_count
        recorded_calls.append(kwargs)
        round_count += 1
        if round_count <= 3:
            return make_response(content="", tool_calls=(make_tool_call(f"c{round_count}", "ghost", "{}"),))
        return make_response(content="Final after abort")

    database = _MemoryDatabase()
    with set_execution_context(_make_context(database)):
        await execute_tool_loop(
            invoke_llm=mock_invoke,
            tool_schemas=TOOL_SCHEMAS,
            tool_lookup={"search_tool": SearchTool()},
            tool_choice="auto",
            max_tool_rounds=10,
            purpose="test",
            expected_cost=None,
            core_messages=[CoreMessage(role=Role.USER, content="test")],
            context_count=0,
            effective_options=None,
            substitutor=None,
            build_tool_result_message=_build_msg,
        )

    assert round_count == 4
    forced_final_msgs = recorded_calls[-1]["core_messages"]
    assert forced_final_msgs[-1].role == Role.USER
    assert "do not call" in forced_final_msgs[-1].content.lower()
    assert forced_final_msgs[-2].role == Role.TOOL  # error from last unknown-tool round


# ── Steering message tests ────────────────────────────────────────────────────


async def test_forced_final_steering_message_ordering_after_max_rounds() -> None:
    """Core message ordering after max_tool_rounds: last is USER steering,
    second-to-last is the TOOL result from the final tool round."""
    recorded_calls: list[dict[str, Any]] = []
    round_count = 0

    async def mock_invoke(**kwargs: Any) -> ModelResponse[Any]:
        nonlocal round_count
        recorded_calls.append(kwargs)
        round_count += 1
        if round_count <= 2:
            return make_response(content="", tool_calls=(make_tool_call(f"c{round_count}", "search_tool", '{"query": "q"}'),))
        return make_response(content="Final synthesis")

    database = _MemoryDatabase()
    with set_execution_context(_make_context(database)):
        await execute_tool_loop(
            invoke_llm=mock_invoke,
            tool_schemas=TOOL_SCHEMAS,
            tool_lookup={"search_tool": SearchTool()},
            tool_choice="auto",
            max_tool_rounds=2,
            purpose="test",
            expected_cost=None,
            core_messages=[CoreMessage(role=Role.USER, content="test")],
            context_count=0,
            effective_options=None,
            substitutor=None,
            build_tool_result_message=_build_msg,
        )

    forced_final_msgs = recorded_calls[-1]["core_messages"]
    assert forced_final_msgs[-1].role == Role.USER
    assert "do not call" in forced_final_msgs[-1].content.lower()
    assert forced_final_msgs[-2].role == Role.TOOL
    # round_index for forced final should be max_tool_rounds + 1 = 3
    assert recorded_calls[-1]["round_index"] == 3


async def test_forced_final_round_index_uses_round_num_after_early_break() -> None:
    """round_index for forced final call must be round_num+1, not max_tool_rounds+1.

    After early unknown-tool break at round 3, forced final should use round_index=4,
    not round_index=11 (max_tool_rounds + 1).
    """
    recorded_calls: list[dict[str, Any]] = []
    round_count = 0

    async def mock_invoke(**kwargs: Any) -> ModelResponse[Any]:
        nonlocal round_count
        recorded_calls.append(kwargs)
        round_count += 1
        if round_count <= 3:
            return make_response(content="", tool_calls=(make_tool_call(f"c{round_count}", "ghost", "{}"),))
        return make_response(content="Final after abort")

    database = _MemoryDatabase()
    with set_execution_context(_make_context(database)):
        await execute_tool_loop(
            invoke_llm=mock_invoke,
            tool_schemas=TOOL_SCHEMAS,
            tool_lookup={"search_tool": SearchTool()},
            tool_choice="auto",
            max_tool_rounds=10,
            purpose="test",
            expected_cost=None,
            core_messages=[CoreMessage(role=Role.USER, content="test")],
            context_count=0,
            effective_options=None,
            substitutor=None,
            build_tool_result_message=_build_msg,
        )

    assert recorded_calls[-1]["round_index"] == 4  # round 3 + 1, not 10 + 1


async def test_forced_final_failure_preserves_tool_records_with_steering() -> None:
    """When the forced final call fails, tool records from prior rounds and the
    fallback response must be preserved. The steering message is in core_messages
    only, not in accumulated_messages."""
    recorded_calls: list[dict[str, Any]] = []
    round_count = 0

    async def mock_invoke(**kwargs: Any) -> ModelResponse[Any]:
        nonlocal round_count
        recorded_calls.append(kwargs)
        round_count += 1
        if round_count == 1:
            return make_response(content="", tool_calls=(make_tool_call("c1", "search_tool", '{"query": "q"}'),))
        raise LLMError("Forced final failed")

    database = _MemoryDatabase()
    with set_execution_context(_make_context(database)):
        msgs, response, records = await execute_tool_loop(
            invoke_llm=mock_invoke,
            tool_schemas=TOOL_SCHEMAS,
            tool_lookup={"search_tool": SearchTool()},
            tool_choice="auto",
            max_tool_rounds=1,
            purpose="test",
            expected_cost=None,
            core_messages=[CoreMessage(role=Role.USER, content="test")],
            context_count=0,
            effective_options=None,
            substitutor=None,
            build_tool_result_message=_build_msg,
        )

    assert len(records) == 1  # tool record from round 1 preserved
    assert "[Final synthesis failed" in response.content
    # Steering message was in core_messages for the forced final call
    forced_final_msgs = recorded_calls[-1]["core_messages"]
    assert forced_final_msgs[-1].role == Role.USER
    assert "do not call" in forced_final_msgs[-1].content.lower()
