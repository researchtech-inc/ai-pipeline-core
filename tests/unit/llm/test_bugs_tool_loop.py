"""Regression tests for forced-final behavior in llm._engine."""

from pydantic import BaseModel, Field

from ai_pipeline_core._llm_core.types import CoreMessage, Role
from ai_pipeline_core.exceptions import LLMError
from ai_pipeline_core.llm import _engine
from ai_pipeline_core.llm._conversation_messages import ToolResultMessage, UserMessage
from ai_pipeline_core.llm._engine import InteractionRequest, ToolRuntime, _tool_loop
from ai_pipeline_core.llm.tools import Tool

from tests.support.helpers import make_tool_call

from .conftest import make_response
from tests.support.model_catalog import DEFAULT_TEST_MODEL


class SearchTool(Tool):
    """Search the web."""

    class Input(BaseModel):
        query: str = Field(description="Search query")

    class Output(BaseModel):
        results: str

    async def run(self, input: Input) -> Output:
        return self.Output(results=f"Results for: {input.query}")


def _request(*, max_rounds: int) -> InteractionRequest:
    return InteractionRequest(
        messages=(CoreMessage(role=Role.USER, content="search for test"),),
        model=DEFAULT_TEST_MODEL,
        tools=ToolRuntime(
            schemas=({"type": "function", "function": {"name": "search_tool", "parameters": {}}},),
            instances={"search_tool": SearchTool()},
            choice="auto",
            max_rounds=max_rounds,
        ),
    )


async def test_forced_final_omits_tools(monkeypatch) -> None:
    """When max rounds are exhausted, the forced-final LLM call has no tool runtime."""
    recorded_runtimes: list[ToolRuntime | None] = []
    round_count = 0

    async def fake_single_call(request, messages, round_index, runtime):
        nonlocal round_count
        _ = (request, messages, round_index)
        recorded_runtimes.append(runtime)
        round_count += 1
        if round_count == 1:
            return make_response(content="", tool_calls=(make_tool_call("c1", "search_tool", '{"query": "test"}'),))
        return make_response(content="Final synthesis")

    monkeypatch.setattr(_engine, "_single_call", fake_single_call)

    await _tool_loop(_request(max_rounds=1), [CoreMessage(role=Role.USER, content="search for test")])

    assert recorded_runtimes[0] is not None
    assert recorded_runtimes[-1] is None


async def test_forced_final_failure_preserves_tool_records(monkeypatch) -> None:
    """Forced-final failure returns a synthetic response and keeps prior tool records."""
    round_count = 0

    async def fake_single_call(request, messages, round_index, runtime):
        nonlocal round_count
        _ = (request, messages, round_index, runtime)
        round_count += 1
        if round_count == 1:
            return make_response(content="", tool_calls=(make_tool_call("c1", "search_tool", '{"query": "deep research"}'),))
        raise LLMError("Empty response content")

    monkeypatch.setattr(_engine, "_single_call", fake_single_call)

    response, records, accumulated, _rounds = await _tool_loop(_request(max_rounds=1), [CoreMessage(role=Role.USER, content="research")])

    assert len(records) == 1
    assert records[0].tool is SearchTool
    assert "Final synthesis failed" in response.content
    assert accumulated[-1] is response


async def test_forced_final_failure_preserves_accumulated_messages(monkeypatch) -> None:
    """Accumulated assistant and tool messages from successful rounds survive synthetic fallback."""
    round_count = 0

    async def fake_single_call(request, messages, round_index, runtime):
        nonlocal round_count
        _ = (request, messages, round_index, runtime)
        round_count += 1
        if round_count <= 2:
            return make_response(content="", tool_calls=(make_tool_call(f"c{round_count}", "search_tool", '{"query": "q"}'),))
        raise LLMError("Forced final failed")

    monkeypatch.setattr(_engine, "_single_call", fake_single_call)

    _response, records, accumulated, _rounds = await _tool_loop(_request(max_rounds=2), [CoreMessage(role=Role.USER, content="multi-round")])

    assert len(records) == 2
    assert sum(1 for item in accumulated if isinstance(item, ToolResultMessage)) == 2


async def test_unknown_tool_rounds_have_limit(monkeypatch) -> None:
    """Loop aborts after three consecutive all-unknown tool rounds, then forces final."""
    round_count = 0

    async def fake_single_call(request, messages, round_index, runtime):
        nonlocal round_count
        _ = (request, messages, round_index, runtime)
        round_count += 1
        if round_count <= 10:
            return make_response(content="", tool_calls=(make_tool_call(f"c{round_count}", "ghost_tool", "{}"),))
        return make_response(content="Forced final")

    monkeypatch.setattr(_engine, "_single_call", fake_single_call)

    await _tool_loop(_request(max_rounds=10), [CoreMessage(role=Role.USER, content="test")])

    assert round_count == 4


async def test_forced_final_has_steering_user_message(monkeypatch) -> None:
    """Forced-final call appends a temporary USER steering message."""
    recorded_messages: list[list[CoreMessage]] = []
    round_count = 0

    async def fake_single_call(request, messages, round_index, runtime):
        nonlocal round_count
        _ = (request, round_index, runtime)
        recorded_messages.append(list(messages))
        round_count += 1
        if round_count == 1:
            return make_response(content="", tool_calls=(make_tool_call("c1", "search_tool", '{"query": "test"}'),))
        return make_response(content="done")

    monkeypatch.setattr(_engine, "_single_call", fake_single_call)

    _response, _records, accumulated, _rounds = await _tool_loop(_request(max_rounds=1), [CoreMessage(role=Role.USER, content="search for test")])

    forced_final_messages = recorded_messages[-1]
    assert forced_final_messages[-1].role == Role.USER
    assert "do not call" in str(forced_final_messages[-1].content).lower()
    assert not any(isinstance(message, UserMessage) for message in accumulated)


async def test_unknown_tool_counter_resets_on_valid_call(monkeypatch) -> None:
    """A valid tool call between unknown rounds resets the consecutive counter."""
    round_count = 0

    async def fake_single_call(request, messages, round_index, runtime):
        nonlocal round_count
        _ = (request, messages, round_index, runtime)
        round_count += 1
        if round_count in (1, 2):
            return make_response(content="", tool_calls=(make_tool_call(f"c{round_count}", "ghost", "{}"),))
        if round_count == 3:
            return make_response(content="", tool_calls=(make_tool_call("c3", "search_tool", '{"query": "x"}'),))
        if round_count in (4, 5):
            return make_response(content="", tool_calls=(make_tool_call(f"c{round_count}", "ghost", "{}"),))
        return make_response(content="Done")

    monkeypatch.setattr(_engine, "_single_call", fake_single_call)

    response, records, _accumulated, _rounds = await _tool_loop(_request(max_rounds=10), [CoreMessage(role=Role.USER, content="test")])

    assert round_count == 6
    assert len(records) == 1
    assert response.content == "Done"
