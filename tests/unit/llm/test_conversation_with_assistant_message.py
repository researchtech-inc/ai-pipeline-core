"""Tests for Conversation.with_assistant_message()."""

import pytest

from ai_pipeline_core._llm_core import LLMRequest
from ai_pipeline_core._llm_core.types import CoreMessage, Role
from ai_pipeline_core.llm._conversation_runtime import to_core_messages
from ai_pipeline_core.llm.conversation import Conversation, AssistantMessage
from tests.support.helpers import ConcreteDocument, create_test_model_response
from tests.support.model_catalog import DEFAULT_TEST_MODEL


def _make_fake_generate(content: str = "response"):
    """Create a fake generate function that returns a canned response."""

    async def fake_generate(request: LLMRequest):
        _ = request
        return create_test_model_response(content=content)

    return fake_generate


class TestWithAssistantMessage:
    """Unit tests for with_assistant_message."""

    def test_returns_new_instance(self):
        """with_assistant_message returns a new Conversation, original unchanged."""
        conv = Conversation(model=DEFAULT_TEST_MODEL)
        new_conv = conv.with_assistant_message("Hello from assistant")

        assert len(conv.messages) == 0
        assert len(new_conv.messages) == 1

    def test_message_is_assistant_message_type(self):
        """The appended message is an AssistantMessage."""
        conv = Conversation(model=DEFAULT_TEST_MODEL)
        new_conv = conv.with_assistant_message("analysis result")

        assert isinstance(new_conv.messages[0], AssistantMessage)
        assert new_conv.messages[0].text == "analysis result"

    def test_preserves_existing_state(self):
        """with_assistant_message preserves model, context, options, substitutor settings."""
        doc = ConcreteDocument(name="ctx.txt", content=b"context")
        conv = Conversation(model=DEFAULT_TEST_MODEL, context=[doc], enable_substitutor=False)
        new_conv = conv.with_assistant_message("msg")

        assert new_conv.model == DEFAULT_TEST_MODEL
        assert new_conv.context == (doc,)
        assert new_conv.enable_substitutor is False

    def test_multiple_assistant_messages(self):
        """Multiple with_assistant_message calls chain correctly."""
        conv = Conversation(model=DEFAULT_TEST_MODEL)
        conv = conv.with_assistant_message("first")
        conv = conv.with_assistant_message("second")

        assert len(conv.messages) == 2
        assert conv.messages[0].text == "first"
        assert conv.messages[1].text == "second"

    def test_empty_string(self):
        """Empty string is accepted (valid edge case for placeholder turns)."""
        conv = Conversation(model=DEFAULT_TEST_MODEL)
        new_conv = conv.with_assistant_message("")
        assert new_conv.messages[0].text == ""

    def test_converts_to_assistant_role_core_message(self):
        """to_core_messages converts AssistantMessage to CoreMessage with ASSISTANT role."""
        conv = Conversation(model=DEFAULT_TEST_MODEL)
        conv = conv.with_assistant_message("injected response")

        core_msgs = to_core_messages(conv.messages, conv.model)

        assert len(core_msgs) == 1
        assert core_msgs[0].role == Role.ASSISTANT
        assert core_msgs[0].content == "injected response"

    def test_interleaved_with_documents(self):
        """Assistant messages interleave correctly with documents in core message order."""
        doc = ConcreteDocument(name="q.txt", content=b"What is 2+2?")
        conv = Conversation(model=DEFAULT_TEST_MODEL)
        conv = conv.with_document(doc)
        conv = conv.with_assistant_message("The answer is 4.")

        core_msgs = to_core_messages(conv.messages, conv.model)

        assert len(core_msgs) == 2
        assert core_msgs[0].role == Role.USER
        assert core_msgs[1].role == Role.ASSISTANT
        assert core_msgs[1].content == "The answer is 4."

    @pytest.mark.asyncio
    async def test_send_after_assistant_message(self, monkeypatch):
        """send() works after with_assistant_message — model sees full history."""
        captured_messages: list[CoreMessage] = []

        async def fake_generate(request: LLMRequest):
            captured_messages.extend(request.messages)
            return create_test_model_response(content="follow-up response")

        monkeypatch.setattr("ai_pipeline_core.llm._engine.generate", fake_generate)

        conv = Conversation(model=DEFAULT_TEST_MODEL, enable_substitutor=False)
        conv = conv.with_assistant_message("Prior analysis: X is Y")
        conv = await conv.send("Based on your analysis, what is X?")

        # Model should see: assistant message, then user question
        roles = [m.role for m in captured_messages]
        assert Role.ASSISTANT in roles
        assert Role.USER in roles
        # Assistant message should come before the user message
        assistant_idx = next(i for i, m in enumerate(captured_messages) if m.role == Role.ASSISTANT)
        user_idx = next(i for i, m in enumerate(captured_messages) if m.role == Role.USER)
        assert assistant_idx < user_idx

    @pytest.mark.asyncio
    async def test_cross_conversation_transfer(self, monkeypatch):
        """Transfer content from conv_a to conv_b via with_assistant_message."""
        call_count = 0

        async def fake_generate(request: LLMRequest):
            _ = request
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return create_test_model_response(content="Analysis: the document discusses topic Z")
            return create_test_model_response(content="Based on the analysis, Z is important")

        monkeypatch.setattr("ai_pipeline_core.llm._engine.generate", fake_generate)

        # Conv A does analysis
        conv_a = Conversation(model=DEFAULT_TEST_MODEL, enable_substitutor=False)
        conv_a = await conv_a.send("Analyze this")

        # Conv B receives the analysis
        conv_b = Conversation(model=DEFAULT_TEST_MODEL, enable_substitutor=False)
        conv_b = conv_b.with_assistant_message(conv_a.content)
        conv_b = await conv_b.send("What did you find?")

        assert conv_b.content == "Based on the analysis, Z is important"
