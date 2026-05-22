"""Deterministic unit coverage for reasoning carriers across the LLM core.

Pins three behaviors the integration suite can only smoke-test honestly:

1. ``to_core_messages`` preserves ``provider_specific_fields`` and
   ``thinking_blocks`` when rebuilding a prior ``ModelResponse`` as an
   ASSISTANT ``CoreMessage`` for the next-turn payload.
2. ``core_messages_to_api`` emits those carriers on ASSISTANT tool-call
   messages, including per-tool-call ``provider_specific_fields`` and the
   ``reasoning_items`` projection.
3. ``strip_reasoning_signatures`` removes ``thought_signature(s)`` keys
   and ``__thought__``-encoded id suffixes coherently, including the
   matching TOOL ``tool_call_id`` remap, and is idempotent.
"""

from ai_pipeline_core._llm_core import CoreMessage, RawToolCall, Role, TokenUsage
from ai_pipeline_core._llm_core._transport import (
    core_messages_to_api,
    strip_reasoning_signatures,
)
from ai_pipeline_core._llm_core.model_response import ModelResponse
from ai_pipeline_core.llm._conversation_runtime import to_core_messages
from tests.support.model_catalog import DEFAULT_TEST_MODEL


def _make_response(
    *,
    content: str = "",
    tool_calls: tuple[RawToolCall, ...] = (),
    provider_specific_fields: dict[str, object] | None = None,
    thinking_blocks: tuple[dict[str, object], ...] | None = None,
) -> ModelResponse[str]:
    return ModelResponse[str](
        content=content,
        parsed=content,
        usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        model=DEFAULT_TEST_MODEL.name,
        response_id="resp-test",
        tool_calls=tool_calls,
        provider_specific_fields=provider_specific_fields,
        thinking_blocks=thinking_blocks,
    )


class TestToCoreMessagesPreservesCarriers:
    """ModelResponse → CoreMessage must carry reasoning state forward."""

    def test_assistant_message_carries_psf_and_thinking_blocks(self) -> None:
        response = _make_response(
            content="answer",
            provider_specific_fields={"reasoning_items": [{"text": "step-1"}], "thought_signatures": "abc"},
            thinking_blocks=({"type": "thinking", "thinking": "hmm"},),
        )
        core_messages = to_core_messages((response,), DEFAULT_TEST_MODEL)
        assert len(core_messages) == 1
        assistant = core_messages[0]
        assert assistant.role == Role.ASSISTANT
        assert assistant.provider_specific_fields == {
            "reasoning_items": [{"text": "step-1"}],
            "thought_signatures": "abc",
        }
        assert assistant.thinking_blocks == ({"type": "thinking", "thinking": "hmm"},)

    def test_tool_call_response_carries_carriers_too(self) -> None:
        call = RawToolCall(
            id="call_1",
            function_name="lookup",
            arguments='{"q": "x"}',
            provider_specific_fields={"thought_signature": "sig-1"},
        )
        response = _make_response(
            content="",
            tool_calls=(call,),
            provider_specific_fields={"reasoning_items": [{"text": "step"}]},
            thinking_blocks=({"type": "thinking", "thinking": "z"},),
        )
        core_messages = to_core_messages((response,), DEFAULT_TEST_MODEL)
        assistant = core_messages[0]
        assert assistant.tool_calls == (call,)
        assert assistant.provider_specific_fields == {"reasoning_items": [{"text": "step"}]}
        assert assistant.thinking_blocks == ({"type": "thinking", "thinking": "z"},)


class TestCoreMessagesToApiCarriers:
    """API conversion must emit reasoning carriers on tool-call ASSISTANT messages."""

    def test_tool_call_assistant_carries_psf_thinking_and_reasoning_items(self) -> None:
        call = RawToolCall(
            id="call_a",
            function_name="lookup",
            arguments='{"q": "x"}',
            provider_specific_fields={"thought_signature": "sig-A"},
        )
        message = CoreMessage(
            role=Role.ASSISTANT,
            content="",
            tool_calls=(call,),
            provider_specific_fields={"reasoning_items": [{"text": "step-1"}], "extra": "kept"},
            thinking_blocks=({"type": "thinking", "thinking": "internal"},),
        )

        api_messages = core_messages_to_api([message], max_inline_file_total_bytes=50_000_000)

        assert len(api_messages) == 1
        entry = api_messages[0]
        assert entry["role"] == "assistant"
        # provider_specific_fields round-trip (whole dict preserved).
        assert entry["provider_specific_fields"] == {"reasoning_items": [{"text": "step-1"}], "extra": "kept"}
        # _apply_reasoning_items projects reasoning_items to a top-level key
        # for providers that consume it that way.
        assert entry["reasoning_items"] == [{"text": "step-1"}]
        # thinking_blocks come through as a tuple of dicts.
        assert entry["thinking_blocks"] == ({"type": "thinking", "thinking": "internal"},)
        # Tool-call provider_specific_fields preserved.
        assert entry["tool_calls"][0]["provider_specific_fields"] == {"thought_signature": "sig-A"}

    def test_assistant_text_only_carries_psf_and_thinking_blocks(self) -> None:
        """Pure text ASSISTANT messages with reasoning carriers still emit them."""
        message = CoreMessage(
            role=Role.ASSISTANT,
            content="text answer",
            provider_specific_fields={"reasoning_items": [{"text": "step"}], "thought_signatures": "sig"},
            thinking_blocks=({"type": "thinking", "thinking": "hmm"},),
        )

        api_messages = core_messages_to_api([message], max_inline_file_total_bytes=50_000_000)

        assert len(api_messages) == 1
        entry = api_messages[0]
        assert entry["provider_specific_fields"] == {"reasoning_items": [{"text": "step"}], "thought_signatures": "sig"}
        assert entry["reasoning_items"] == [{"text": "step"}]
        assert entry["thinking_blocks"] == ({"type": "thinking", "thinking": "hmm"},)


class TestStripReasoningSignatures:
    """strip_reasoning_signatures must remove signatures everywhere they hide."""

    def test_removes_message_level_signature_keys(self) -> None:
        message = CoreMessage(
            role=Role.ASSISTANT,
            content="answer",
            provider_specific_fields={
                "thought_signature": "abc",
                "thought_signatures": "xyz",
                "reasoning_items": [{"text": "keep"}],
            },
        )
        result = strip_reasoning_signatures((message,))

        assert len(result) == 1
        cleaned_psf = result[0].provider_specific_fields
        # reasoning_items survives; signature keys are gone.
        assert cleaned_psf == {"reasoning_items": [{"text": "keep"}]}

    def test_drops_psf_when_only_signatures_present(self) -> None:
        message = CoreMessage(
            role=Role.ASSISTANT,
            content="answer",
            provider_specific_fields={"thought_signature": "abc"},
        )
        result = strip_reasoning_signatures((message,))
        assert result[0].provider_specific_fields is None

    def test_removes_tool_call_signature_keys_and_thought_suffix(self) -> None:
        call = RawToolCall(
            id="call_1__thought__sig-blob",
            function_name="lookup",
            arguments='{"q": "x"}',
            provider_specific_fields={"thought_signature": "blob", "other": "kept"},
        )
        message = CoreMessage(role=Role.ASSISTANT, content="", tool_calls=(call,))
        result = strip_reasoning_signatures((message,))

        assert len(result) == 1
        cleaned_call = result[0].tool_calls[0]  # type: ignore[index]  # guaranteed non-empty by prior assertion
        # __thought__ suffix is stripped from the id.
        assert cleaned_call.id == "call_1"
        # Signature key dropped, unrelated key survives.
        assert cleaned_call.provider_specific_fields == {"other": "kept"}

    def test_remaps_matching_tool_message_id(self) -> None:
        call = RawToolCall(
            id="call_42__thought__blob",
            function_name="lookup",
            arguments='{"q": "x"}',
            provider_specific_fields={"thought_signature": "blob"},
        )
        assistant = CoreMessage(role=Role.ASSISTANT, content="", tool_calls=(call,))
        tool_result = CoreMessage(role=Role.TOOL, content="ok", tool_call_id="call_42__thought__blob", name="lookup")
        unrelated_tool = CoreMessage(role=Role.TOOL, content="ok", tool_call_id="other_call", name="other")

        result = strip_reasoning_signatures((assistant, tool_result, unrelated_tool))

        # Matching TOOL.tool_call_id is remapped to the stripped form.
        assert result[1].tool_call_id == "call_42"
        # Unrelated TOOL.tool_call_id is left alone.
        assert result[2].tool_call_id == "other_call"

    def test_idempotent_on_clean_messages(self) -> None:
        message = CoreMessage(
            role=Role.ASSISTANT, content="plain", provider_specific_fields={"reasoning_items": [{"text": "x"}]}
        )
        original = (message,)
        result = strip_reasoning_signatures(original)
        # Nothing to strip → must return the same tuple identity.
        assert result is original

    def test_idempotent_on_second_call(self) -> None:
        call = RawToolCall(
            id="call_1__thought__sig",
            function_name="lookup",
            arguments="{}",
            provider_specific_fields={"thought_signature": "x"},
        )
        message = CoreMessage(role=Role.ASSISTANT, content="", tool_calls=(call,))
        once = strip_reasoning_signatures((message,))
        twice = strip_reasoning_signatures(once)
        # A second call has nothing left to strip → must return same tuple identity.
        assert twice is once

    def test_non_assistant_messages_untouched(self) -> None:
        user = CoreMessage(role=Role.USER, content="ask")
        result = strip_reasoning_signatures((user,))
        # USER message can't carry signatures; tuple identity preserved.
        assert result is (user,) or result == (user,)
