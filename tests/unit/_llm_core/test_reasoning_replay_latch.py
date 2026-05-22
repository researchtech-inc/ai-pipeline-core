"""Unit coverage for the ProviderReasoningReplayError retry latch.

When an upstream rejects a replayed reasoning signature (Vertex ``thought_signature``
or peer), the client must:

1. Strip every signature carrier from the messages on the next attempt.
2. Pin routing to the offending deployment via ``force_deployment_id`` so a
   sibling deployment doesn't reject the (already-stripped) request.
3. Latch the strip+pin state for every subsequent retry on the same model,
   including retries triggered by transient transport failures.
4. Stamp the final successful response's transport metadata with
   ``reasoning_replay_stripped=True``.
"""

import pytest

from ai_pipeline_core._llm_core import (
    CoreMessage,
    LLMRequest,
    RawToolCall,
    RetrySpec,
    Role,
    TokenUsage,
)
from ai_pipeline_core._llm_core import client as _client
from ai_pipeline_core._llm_core._routing import _CallContext
from ai_pipeline_core._llm_core.exceptions import (
    EmptyResponseError,
    ProviderReasoningReplayError,
)
from ai_pipeline_core._llm_core.model_response import AttemptOutcome, ModelResponse
from ai_pipeline_core._llm_core.request import AttemptRequest
from tests.support.model_catalog import DEFAULT_TEST_MODEL


def _make_request() -> LLMRequest:
    call = RawToolCall(
        id="call_1__thought__sig",
        function_name="lookup",
        arguments="{}",
        provider_specific_fields={"thought_signature": "sig"},
    )
    messages = (
        CoreMessage(role=Role.USER, content="hi"),
        CoreMessage(
            role=Role.ASSISTANT,
            content="",
            tool_calls=(call,),
            provider_specific_fields={"thought_signature": "outer"},
        ),
        CoreMessage(role=Role.TOOL, content="ok", tool_call_id="call_1__thought__sig", name="lookup"),
    )
    return LLMRequest(
        model=DEFAULT_TEST_MODEL,
        messages=messages,
        retry=RetrySpec(retries=3, retry_delay_seconds=0.0),
    )


def _make_response() -> ModelResponse[str]:
    return ModelResponse[str](
        content="ok",
        parsed="ok",
        usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        model=DEFAULT_TEST_MODEL.name,
        response_id="r-1",
    )


class TestReasoningReplayLatch:
    @pytest.mark.asyncio
    async def test_strip_and_pin_latch_across_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[AttemptRequest] = []
        outcomes: list[AttemptOutcome] = [
            # Attempt 0: original messages produce a signature-replay rejection.
            AttemptOutcome(
                error=ProviderReasoningReplayError("rejected", original=None, deployment_id="deployment-a"),
                failed_deployment_id="deployment-a",
            ),
            # Attempt 1: stripped + pinned, but a transient transport error.
            AttemptOutcome(error=EmptyResponseError("transient")),
            # Attempt 2: stripped + pinned, success.
            AttemptOutcome(response=_make_response()),
        ]

        async def fake_execute_attempt(attempt: AttemptRequest) -> AttemptOutcome:
            captured.append(attempt)
            return outcomes.pop(0)

        monkeypatch.setattr(_client, "_execute_attempt", fake_execute_attempt)

        ctx = _CallContext(_make_request())
        result = await _client._try_one_model(ctx, DEFAULT_TEST_MODEL)

        # Three attempts captured, in order.
        assert len(captured) == 3

        # Attempt 0: pristine messages (no strip yet, no pin yet).
        first = captured[0]
        assert first.call.routing.force_deployment_id is None
        first_assistant = next(msg for msg in first.call.messages if msg.role == Role.ASSISTANT)
        assert first_assistant.provider_specific_fields == {"thought_signature": "outer"}
        first_call = first_assistant.tool_calls[0]  # type: ignore[index]
        assert first_call.id == "call_1__thought__sig"
        assert first_call.provider_specific_fields == {"thought_signature": "sig"}

        # Attempt 1: latched — signatures stripped, deployment pinned.
        second = captured[1]
        assert second.call.routing.force_deployment_id == "deployment-a"
        second_assistant = next(msg for msg in second.call.messages if msg.role == Role.ASSISTANT)
        # Whole psf dropped because only signature keys were present.
        assert second_assistant.provider_specific_fields is None
        second_call = second_assistant.tool_calls[0]  # type: ignore[index]
        assert second_call.id == "call_1"
        assert second_call.provider_specific_fields is None
        # Matching TOOL message tool_call_id also remapped.
        second_tool = next(msg for msg in second.call.messages if msg.role == Role.TOOL)
        assert second_tool.tool_call_id == "call_1"

        # Attempt 2: latch still holds across the transient retry.
        third = captured[2]
        assert third.call.routing.force_deployment_id == "deployment-a"
        third_assistant = next(msg for msg in third.call.messages if msg.role == Role.ASSISTANT)
        assert third_assistant.provider_specific_fields is None
        third_call = third_assistant.tool_calls[0]  # type: ignore[index]
        assert third_call.id == "call_1"

        # Result response is stamped with reasoning_replay_stripped=True.
        assert result.response is not None
        assert result.response.transport.aipl.reasoning_replay_stripped is True

    @pytest.mark.asyncio
    async def test_no_latch_without_replay_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without a ProviderReasoningReplayError trigger, signatures are not stripped."""
        captured: list[AttemptRequest] = []
        outcomes: list[AttemptOutcome] = [
            AttemptOutcome(error=EmptyResponseError("transient")),
            AttemptOutcome(response=_make_response()),
        ]

        async def fake_execute_attempt(attempt: AttemptRequest) -> AttemptOutcome:
            captured.append(attempt)
            return outcomes.pop(0)

        monkeypatch.setattr(_client, "_execute_attempt", fake_execute_attempt)

        ctx = _CallContext(_make_request())
        result = await _client._try_one_model(ctx, DEFAULT_TEST_MODEL)

        assert len(captured) == 2
        # Neither attempt was stripped or pinned.
        for attempt in captured:
            assert attempt.call.routing.force_deployment_id is None
            assistant = next(msg for msg in attempt.call.messages if msg.role == Role.ASSISTANT)
            assert assistant.provider_specific_fields == {"thought_signature": "outer"}
            assert assistant.tool_calls[0].id == "call_1__thought__sig"  # type: ignore[union-attr]
        # Response not stamped.
        assert result.response is not None
        assert result.response.transport.aipl.reasoning_replay_stripped is False

    @pytest.mark.asyncio
    async def test_stamp_helper_marks_response(self) -> None:
        """_stamp_reasoning_replay_stripped flips the AIPL flag without losing other transport state."""
        original = AttemptOutcome(response=_make_response())
        stamped = _client._stamp_reasoning_replay_stripped(original)
        assert stamped.response is not None
        assert stamped.response.transport.aipl.reasoning_replay_stripped is True
        # Original outcome untouched (frozen dataclass + model_copy semantics).
        assert original.response is not None
        assert original.response.transport.aipl.reasoning_replay_stripped is False

    def test_with_stripped_reasoning_pins_deployment(self) -> None:
        """_with_stripped_reasoning re-emits the attempt with stripped messages + pinned routing."""
        request = _make_request()
        attempt = AttemptRequest(call=request, model=request.model, attempt_index=1, call_id="cid")

        rebuilt = _client._with_stripped_reasoning(attempt, "deployment-z")

        assert rebuilt.call.routing.force_deployment_id == "deployment-z"
        assistant = next(msg for msg in rebuilt.call.messages if msg.role == Role.ASSISTANT)
        assert assistant.provider_specific_fields is None
        assert assistant.tool_calls[0].id == "call_1"  # type: ignore[union-attr]
        tool_msg = next(msg for msg in rebuilt.call.messages if msg.role == Role.TOOL)
        assert tool_msg.tool_call_id == "call_1"

    def test_with_stripped_reasoning_without_deployment_id_skips_pin(self) -> None:
        request = _make_request()
        attempt = AttemptRequest(call=request, model=request.model, attempt_index=1, call_id="cid")

        rebuilt = _client._with_stripped_reasoning(attempt, None)

        # No deployment id → no force pin (the caller's routing stays as-is).
        assert rebuilt.call.routing.force_deployment_id is None
        # Signature strip still happened.
        assistant = next(msg for msg in rebuilt.call.messages if msg.role == Role.ASSISTANT)
        assert assistant.tool_calls[0].id == "call_1"  # type: ignore[union-attr]
