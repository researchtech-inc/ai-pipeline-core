"""Integration tests for assistant-message conversation state and cost reporting."""

from typing import Any

import pytest

from ai_pipeline_core.llm import AIModel, Conversation
from ai_pipeline_core.settings import settings

HAS_API_KEYS = bool(settings.openai_api_key and settings.openai_base_url)

pytestmark = [
    pytest.mark.skipif(not HAS_API_KEYS, reason="LLM API credentials not configured"),
]


class TestLLMAssistantMessage:
    """Recover assistant-message live regressions."""

    @pytest.mark.asyncio
    async def test_with_assistant_message_cross_conversation(
        self,
        default_test_model: AIModel,
        nonce: str,
        cost_budget: Any,
    ) -> None:
        """An assistant message injected into a new conversation remains usable."""
        conv_a = await Conversation(
            model=default_test_model,
            enable_substitutor=False,
        ).send(
            f"What is the capital of Germany? Reply with one word. Nonce: {nonce}",
            purpose=f"assistant-message-source-{nonce}",
        )
        cost_budget.add(conv_a)
        assert "berlin" in conv_a.content.lower()

        conv_b = Conversation(
            model=default_test_model,
            enable_substitutor=False,
        ).with_assistant_message(conv_a.content)
        conv_b = await conv_b.send(
            "What city did you just mention? Reply with one word.",
            purpose=f"assistant-message-followup-{nonce}",
        )
        cost_budget.add(conv_b)

        assert "berlin" in conv_b.content.lower()

    @pytest.mark.asyncio
    async def test_cost_returned_for_all_providers(
        self,
        test_model_choice: AIModel,
        nonce: str,
        cost_budget: Any,
    ) -> None:
        """Tracked live responses report a non-zero cost across catalog models."""
        conv = await Conversation(
            model=test_model_choice,
            enable_substitutor=False,
        ).send(
            f"Say hello. Nonce: {nonce}",
            purpose=f"assistant-message-cost-{test_model_choice.name}-{nonce}",
        )

        cost_budget.add(conv)
        assert conv.cost is not None, f"{test_model_choice.name}: cost is None"
        assert conv.cost > 0, f"{test_model_choice.name}: cost is {conv.cost}, expected > 0"
