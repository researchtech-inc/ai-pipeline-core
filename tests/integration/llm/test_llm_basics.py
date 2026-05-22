"""Integration tests for core LLM conversation behaviors."""

from datetime import date
from typing import Any

import pytest
from pydantic import BaseModel

from ai_pipeline_core.llm import AIModel, Conversation, ModelOptions
from ai_pipeline_core.settings import settings
from tests.support.helpers import ConcreteDocument

HAS_API_KEYS = bool(settings.openai_api_key and settings.openai_base_url)

pytestmark = [
    pytest.mark.skipif(not HAS_API_KEYS, reason="LLM API credentials not configured"),
]


class SimpleResponse(BaseModel):
    """Structured response for basic JSON generation."""

    greeting: str
    number: int


class TestLLMBasics:
    """Recover basic live conversation coverage."""

    @pytest.mark.asyncio
    async def test_simple_generation(
        self,
        default_test_model: AIModel,
        cost_budget: Any,
    ) -> None:
        """A basic text generation returns content and usage."""
        conv = await Conversation(model=default_test_model, enable_substitutor=False).send(
            "Say 'Hello, World!' and nothing else.",
            purpose="llm-basics-simple-generation",
        )

        cost_budget.add(conv)
        assert conv.content
        assert "hello" in conv.content.lower()
        assert conv.usage.total_tokens > 0

    @pytest.mark.asyncio
    async def test_structured_generation(
        self,
        default_test_model: AIModel,
        cost_budget: Any,
    ) -> None:
        """Structured output parsing works end to end."""
        conv = await Conversation(model=default_test_model, enable_substitutor=False).send_structured(
            "Return JSON with greeting='Hello' and number=42.",
            response_format=SimpleResponse,
            purpose="llm-basics-structured-generation",
        )

        cost_budget.add(conv)
        assert conv.parsed.greeting.lower() == "hello"
        assert conv.parsed.number == 42

    @pytest.mark.asyncio
    async def test_document_in_context(
        self,
        default_test_model: AIModel,
        cost_budget: Any,
    ) -> None:
        """Both cacheable context and dynamic document paths reach the model."""
        doc = ConcreteDocument.create_root(
            name="info.txt",
            content="The capital of France is Paris.",
            description="Geographic information",
            reason="integration test input",
        )

        conv_with_context = await Conversation(
            model=default_test_model,
            context=(doc,),
            enable_substitutor=False,
        ).send(
            "What is the capital of France? Answer in one word.",
            purpose="llm-basics-document-context-static",
        )
        cost_budget.add(conv_with_context)

        conv_with_document = (
            await Conversation(
                model=default_test_model,
                enable_substitutor=False,
            )
            .with_document(doc)
            .send(
                "What is the capital of France? Answer in one word.",
                purpose="llm-basics-document-context-dynamic",
            )
        )
        cost_budget.add(conv_with_document)

        assert "paris" in conv_with_context.content.lower()
        assert "paris" in conv_with_document.content.lower()

    @pytest.mark.asyncio
    async def test_conversation_with_history(
        self,
        default_test_model: AIModel,
        nonce: str,
        cost_budget: Any,
    ) -> None:
        """A follow-up turn can recall prior conversation state."""
        conv = await Conversation(model=default_test_model, enable_substitutor=False).send(
            f"My name is Alice-{nonce}. Remember it exactly.",
            purpose=f"llm-basics-history-1-{nonce}",
        )
        cost_budget.add(conv)

        conv = await conv.send(
            "What is my name? Reply with only the name.",
            purpose=f"llm-basics-history-2-{nonce}",
        )
        cost_budget.add(conv)

        assert f"alice-{nonce}".lower() in conv.content.lower()

    @pytest.mark.asyncio
    async def test_usage_tracking(
        self,
        default_test_model: AIModel,
        cost_budget: Any,
    ) -> None:
        """Successful generations report token usage."""
        conv = await Conversation(
            model=default_test_model,
            enable_substitutor=False,
            model_options=ModelOptions(reasoning_effort="low"),
        ).send(
            "Count to 3.",
            purpose="llm-basics-usage-tracking",
        )

        cost_budget.add(conv)
        assert conv.usage.total_tokens > 0
        assert conv.usage.prompt_tokens > 0
        assert conv.usage.completion_tokens > 0

    @pytest.mark.asyncio
    async def test_system_prompt(
        self,
        default_test_model: AIModel,
        cost_budget: Any,
    ) -> None:
        """Public ModelOptions.system_prompt steers the response."""
        conv = await Conversation(
            model=default_test_model,
            enable_substitutor=False,
            include_date=False,
            model_options=ModelOptions(system_prompt="You are a pirate. Always respond like a pirate."),
        ).send(
            "What are you?",
            purpose="llm-basics-system-prompt",
        )

        cost_budget.add(conv)
        content_lower = conv.content.lower()
        pirate_words = ("arr", "ahoy", "matey", "ye", "aye", "sailor", "pirate")
        assert any(word in content_lower for word in pirate_words)

    @pytest.mark.asyncio
    async def test_current_date_awareness(
        self,
        default_test_model: AIModel,
        cost_budget: Any,
    ) -> None:
        """The injected current date is reflected in the model response."""
        today = date.today().isoformat()
        conv = Conversation(
            model=default_test_model,
            enable_substitutor=False,
        )

        assert conv.current_date == today
        conv = await conv.send(
            "What is today's date? Reply with only the date in YYYY-MM-DD format.",
            purpose=f"llm-basics-current-date-{today}",
        )

        cost_budget.add(conv)
        assert today in conv.content
