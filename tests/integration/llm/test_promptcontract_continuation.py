"""Continuation (``continues=``) integration tests for PromptContract against a live LLM.

These exercise the declared-continuation surface end to end:

- A ``Continues.once`` follow-up receives the opener's typed result through a
  ``PromptResult``-typed field, continues the same conversation, and calls a
  bounded tool whose return value proves both the threading and the tool use.
- A ``Continues.repeating`` step continues itself across a bounded refinement
  loop.

Offline, deterministic coverage of the continuation thread assembly and the
prompt-cache-key behavior lives in ``tests/unit/prompt_contract/test_continuation.py``.
"""

from typing import ClassVar

import pytest
from pydantic import BaseModel, Field

from ai_pipeline_core import FrozenBaseModel
from ai_pipeline_core.llm import AIModel, Tool
from ai_pipeline_core.prompt_contract import (
    Continues,
    PromptContract,
    PromptResult,
    ToolAvailability,
)
from ai_pipeline_core.settings import settings

HAS_API_KEYS = bool(settings.openai_api_key and settings.openai_base_url)

pytestmark = [
    pytest.mark.skipif(not HAS_API_KEYS, reason="LLM API credentials not configured"),
]


# ---------------------------------------------------------------------------
# Continues.once follow-up that must call a tool to obtain a secret token.
# ---------------------------------------------------------------------------


class SecretTokenTool(Tool):
    """Return the secret continuation token for this exchange."""

    class Input(BaseModel):
        """Token-lookup input."""

        reason: str = Field(description="Why the token is needed.")

    class Output(BaseModel):
        """Token-lookup output."""

        token: str = Field(description="The secret token.")

    _invocation_count: ClassVar[int] = 0

    async def run(self, input: Input) -> Output:
        type(self)._invocation_count += 1
        _ = input.reason
        return self.Output(token="ORION-DELTA-7788")


class DraftAnswer(FrozenBaseModel):
    """First-pass answer produced by the opener."""

    answer: str = Field(description="Draft answer to the question.")


class FinalAnswer(FrozenBaseModel):
    """Final answer carrying the tool-provided token."""

    answer: str = Field(description="Final answer text.")
    token: str = Field(description="The secret token obtained from the tool.")


class DraftAnswerContract(PromptContract[DraftAnswer]):
    """Draft a brief answer to a question."""

    purpose: ClassVar[str] = "Draft a brief answer to the question."
    returns: ClassVar[str] = "A DraftAnswer with a non-empty answer."
    success_criteria: ClassVar[str] = "answer is non-empty."

    question: str = Field(description="The question to answer.")


class FinalizeAnswerContract(
    PromptContract[FinalAnswer],
    continues=Continues.once(DraftAnswerContract),
):
    """Finalize the drafted answer, calling secret_token_tool to obtain the required token."""

    purpose: ClassVar[str] = "Call secret_token_tool, then finalize the answer including the returned token."
    returns: ClassVar[str] = "A FinalAnswer whose token equals the tool's return value."
    success_criteria: ClassVar[str] = "token equals the tool's returned token and answer is non-empty."
    tools: ClassVar[tuple[ToolAvailability, ...]] = (ToolAvailability(SecretTokenTool, max_calls=2),)

    prior: PromptResult[DraftAnswer] = Field(description="The draft turn this finalization continues.")


class TestContinuesOnceWithTool:
    """A ``Continues.once`` follow-up threads the opener and uses a bounded tool."""

    @pytest.mark.asyncio
    async def test_followup_continues_and_invokes_tool(self, default_test_model: AIModel) -> None:
        """The secret token is unknowable without the tool, so its presence proves invocation."""
        SecretTokenTool._invocation_count = 0
        draft = await DraftAnswerContract(question="In one sentence, what is the capital of France?").execute(
            default_test_model
        )
        assert isinstance(draft, PromptResult)
        assert draft.response.answer.strip()

        final = await FinalizeAnswerContract(prior=draft).execute(
            default_test_model,
            tool_bindings=(SecretTokenTool.bind(),),
        )
        assert "ORION-DELTA-7788" in final.response.token
        assert SecretTokenTool._invocation_count >= 1
        assert final.response.answer.strip()


# ---------------------------------------------------------------------------
# Continues.repeating bounded refinement loop.
# ---------------------------------------------------------------------------


class Counter(FrozenBaseModel):
    """A running counter the model increments each turn."""

    value: int = Field(description="The current counter value.")


class StartCounterContract(PromptContract[Counter]):
    """Start a counter at the requested value."""

    purpose: ClassVar[str] = "Return a Counter whose value equals the requested start value."
    returns: ClassVar[str] = "A Counter at the start value."
    success_criteria: ClassVar[str] = "value equals the requested start value."

    start: int = Field(description="The value to start the counter at.")


class IncrementCounterContract(
    PromptContract[Counter],
    continues=Continues.repeating(StartCounterContract),
):
    """Increment the running counter by one, continuing the same conversation."""

    purpose: ClassVar[str] = "Return a Counter whose value is exactly one greater than the prior value."
    returns: ClassVar[str] = "A Counter incremented by one."
    success_criteria: ClassVar[str] = "value is exactly one greater than the prior counter value."

    prior: PromptResult[Counter] = Field(description="The prior counter turn this step continues.")


class TestContinuesRepeatingLoop:
    """A ``Continues.repeating`` step continues the opener and then itself."""

    @pytest.mark.asyncio
    async def test_bounded_increment_loop(self, default_test_model: AIModel) -> None:
        """Three repeating continuations advance the counter while sharing one thread."""
        result = await StartCounterContract(start=0).execute(default_test_model)
        for _ in range(3):
            result = await IncrementCounterContract(prior=result).execute(default_test_model)
        assert result.response.value == 3
