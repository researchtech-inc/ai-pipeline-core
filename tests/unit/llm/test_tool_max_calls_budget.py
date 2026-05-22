"""Per-tool max_calls budget enforcement in the engine.

Stage A semantic change: ``ToolAvailability.max_calls`` caps the total
executions of one tool class across the whole ``PromptContract.execute()``
or ``Conversation.send()`` lifecycle — tool-loop rounds AND repair attempts
combined. Calls beyond the cap return a budget-exhausted ToolOutput
without invoking the tool.
"""

from typing import Any

import pytest
from pydantic import BaseModel, Field

from ai_pipeline_core._llm_core.types import CoreMessage, Role
from ai_pipeline_core.llm import _engine
from ai_pipeline_core.llm._engine import InteractionRequest, ToolRuntime, _tool_loop
from ai_pipeline_core.llm.tools import Tool
from tests.support.helpers import make_tool_call
from tests.support.model_catalog import DEFAULT_TEST_MODEL

from .conftest import make_response


class CounterTool(Tool):
    """Counter tool used to verify per-tool budget enforcement."""

    class Input(BaseModel):
        """Counter input."""

        token: str = Field(description="token")

    class Output(BaseModel):
        """Counter output."""

        echo: str

    def __init__(self) -> None:
        self.invocations = 0

    async def run(self, input: Input) -> Output:
        self.invocations += 1
        return self.Output(echo=input.token)


def _make_request(*, tool: CounterTool, max_calls: int, max_rounds: int = 4) -> InteractionRequest:
    runtime = ToolRuntime(
        schemas=({"type": "function", "function": {"name": "counter_tool", "parameters": {}}},),
        instances={"counter_tool": tool},
        choice="auto",
        max_rounds=max_rounds,
        max_calls_by_name={"counter_tool": max_calls},
    )
    return InteractionRequest(
        messages=(CoreMessage(role=Role.USER, content="run tools"),),
        model=DEFAULT_TEST_MODEL,
        tools=runtime,
    )


@pytest.mark.asyncio
async def test_five_calls_in_one_round_capped_at_two(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model emits 5 tool calls in one response; max_calls=2 → only 2 execute, 3 short-circuit."""
    tool = CounterTool()
    round_count = 0

    async def fake_single_call(request: Any, messages: Any, round_index: int, runtime: Any) -> Any:
        nonlocal round_count
        _ = (request, messages, round_index, runtime)
        round_count += 1
        if round_count == 1:
            return make_response(
                content="",
                tool_calls=tuple(make_tool_call(f"c{i}", "counter_tool", f'{{"token": "t{i}"}}') for i in range(5)),
            )
        return make_response(content="done")

    monkeypatch.setattr(_engine, "_single_call", fake_single_call)

    _, _, accumulated, _ = await _tool_loop(
        _make_request(tool=tool, max_calls=2),
        [CoreMessage(role=Role.USER, content="run tools")],
        remaining_calls={"counter_tool": 2},
    )

    # Only 2 actual tool invocations even though the model asked for 5.
    assert tool.invocations == 2

    # 3 tool-result messages should carry the budget-exhausted error.
    from ai_pipeline_core.llm._conversation_messages import ToolResultMessage

    tool_results = [m for m in accumulated if isinstance(m, ToolResultMessage)]
    exhausted = [m for m in tool_results if "max_calls budget" in m.content]
    assert len(exhausted) == 3


@pytest.mark.asyncio
async def test_budget_persists_across_rounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Round 1 uses 2/3 budget; round 2 uses 1/3; round 3 attempts another call → exhausted."""
    tool = CounterTool()
    round_count = 0

    async def fake_single_call(request: Any, messages: Any, round_index: int, runtime: Any) -> Any:
        nonlocal round_count
        _ = (request, messages, round_index, runtime)
        round_count += 1
        if round_count == 1:
            return make_response(
                content="",
                tool_calls=(
                    make_tool_call("c1", "counter_tool", '{"token": "a"}'),
                    make_tool_call("c2", "counter_tool", '{"token": "b"}'),
                ),
            )
        if round_count == 2:
            return make_response(
                content="",
                tool_calls=(make_tool_call("c3", "counter_tool", '{"token": "c"}'),),
            )
        if round_count == 3:
            return make_response(
                content="",
                tool_calls=(make_tool_call("c4", "counter_tool", '{"token": "d"}'),),
            )
        return make_response(content="done")

    monkeypatch.setattr(_engine, "_single_call", fake_single_call)

    remaining = {"counter_tool": 3}
    _, _, accumulated, _ = await _tool_loop(
        _make_request(tool=tool, max_calls=3, max_rounds=5),
        [CoreMessage(role=Role.USER, content="run tools")],
        remaining_calls=remaining,
    )

    # Tool only ran 3 times; round 3's 4th call was budget-exhausted.
    assert tool.invocations == 3
    assert remaining["counter_tool"] == 0

    from ai_pipeline_core.llm._conversation_messages import ToolResultMessage

    tool_results = [m for m in accumulated if isinstance(m, ToolResultMessage)]
    exhausted = [m for m in tool_results if "max_calls budget" in m.content]
    assert len(exhausted) == 1


@pytest.mark.asyncio
async def test_budget_persists_across_validation_repair_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cross-repair persistence: budget consumed in attempt 1 stays depleted in attempt 2.

    Scripts validate() to reject attempt 1 and accept attempt 2. Attempt 1
    consumes the full ``max_calls=2`` budget; attempt 2 tries the same tool
    again and must hit the budget-exhausted short-circuit — no third real
    execution.
    """
    from ai_pipeline_core._llm_core.request import ValidationSpec
    from ai_pipeline_core.llm._engine import execute_interaction

    tool = CounterTool()
    round_count = 0

    async def fake_single_call(request: Any, messages: Any, round_index: int, runtime: Any) -> Any:
        nonlocal round_count
        _ = (request, messages, round_index, runtime)
        round_count += 1
        if round_count in (1, 3):
            # Attempt 1's first tool round, then attempt 2's first tool round.
            return make_response(
                content="",
                tool_calls=(
                    make_tool_call(f"c{round_count}-a", "counter_tool", '{"token": "a"}'),
                    make_tool_call(f"c{round_count}-b", "counter_tool", '{"token": "b"}'),
                ),
            )
        # Final response in each attempt — model "stops" calling tools.

        class _Parsed(BaseModel):
            ok: bool

        return make_response(content='{"ok": true}').model_copy(update={"parsed": _Parsed(ok=round_count > 2)})

    monkeypatch.setattr(_engine, "_single_call", fake_single_call)

    class _Out(BaseModel):
        ok: bool

    state = {"attempt": 0}

    def validator(parsed: Any) -> tuple[Any, ...]:
        _ = parsed
        state["attempt"] += 1
        if state["attempt"] == 1:
            from ai_pipeline_core.prompt_contract import ValidationFailure

            return (ValidationFailure(message="retry once"),)
        return ()

    runtime = ToolRuntime(
        schemas=({"type": "function", "function": {"name": "counter_tool", "parameters": {}}},),
        instances={"counter_tool": tool},
        choice="auto",
        max_rounds=4,
        max_calls_by_name={"counter_tool": 2},
    )
    request = InteractionRequest(
        messages=(CoreMessage(role=Role.USER, content="run"),),
        model=DEFAULT_TEST_MODEL,
        response_format=_Out,
        tools=runtime,
        validation=ValidationSpec(validate=validator, max_attempts=2),
    )

    await execute_interaction(request)

    # Attempt 1 consumed the full budget (2 invocations); attempt 2 tried 2
    # more, both must be short-circuited by the budget — total real
    # invocations stays at 2.
    assert tool.invocations == 2


@pytest.mark.asyncio
async def test_legacy_unmetered_path_unaffected(monkeypatch: pytest.MonkeyPatch) -> None:
    """When max_calls_by_name is empty (legacy Conversation.send path), no budget cap applies."""
    tool = CounterTool()
    round_count = 0

    async def fake_single_call(request: Any, messages: Any, round_index: int, runtime: Any) -> Any:
        nonlocal round_count
        _ = (request, messages, round_index, runtime)
        round_count += 1
        if round_count == 1:
            return make_response(
                content="",
                tool_calls=tuple(make_tool_call(f"c{i}", "counter_tool", f'{{"token": "t{i}"}}') for i in range(10)),
            )
        return make_response(content="done")

    monkeypatch.setattr(_engine, "_single_call", fake_single_call)

    runtime = ToolRuntime(
        schemas=({"type": "function", "function": {"name": "counter_tool", "parameters": {}}},),
        instances={"counter_tool": tool},
        choice="auto",
        max_rounds=3,
        max_calls_by_name={},
    )
    request = InteractionRequest(
        messages=(CoreMessage(role=Role.USER, content="run"),),
        model=DEFAULT_TEST_MODEL,
        tools=runtime,
    )

    await _tool_loop(
        request,
        [CoreMessage(role=Role.USER, content="run")],
        remaining_calls=None,
    )

    # No cap → all 10 tool calls executed.
    assert tool.invocations == 10
