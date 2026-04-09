"""Integration tests for LLM functionality (requires API keys)."""

import uuid
from datetime import date

import pytest
from pydantic import BaseModel

from ai_pipeline_core.llm import Conversation, ModelOptions
from ai_pipeline_core.settings import settings
from tests.support.helpers import ConcreteDocument

# Skip all tests in this file if API key not available
pytestmark = pytest.mark.integration


# Check if API keys are configured in settings (respects .env file)
HAS_API_KEYS = bool(settings.openai_api_key and settings.openai_base_url)


class ItemList(BaseModel):
    items: list[str]


class SimpleResponse(BaseModel):
    greeting: str
    number: int


@pytest.mark.skipif(not HAS_API_KEYS, reason="OpenAI API keys not configured in settings or .env file")
class TestLLMIntegration:
    """Integration tests that make real LLM calls using Conversation."""

    @pytest.mark.asyncio
    async def test_simple_generation(self):
        """Test basic text generation."""
        conv = Conversation(
            model="gemini-3-flash",
            model_options=ModelOptions(max_completion_tokens=1000),
        )

        conv = await conv.send("Say 'Hello, World!' and nothing else.")

        assert conv.content
        assert "Hello" in conv.content or "hello" in conv.content
        assert conv.usage.total_tokens > 0

    @pytest.mark.asyncio
    async def test_structured_generation(self):
        """Test structured output generation."""
        conv = Conversation(
            model="gemini-3-flash",
            model_options=ModelOptions(max_completion_tokens=1000),
        )

        conv = await conv.send_structured(
            "Return a JSON with greeting='Hello' and number=42",
            response_format=SimpleResponse,
        )

        assert conv.parsed.greeting == "Hello"
        assert conv.parsed.number == 42

    @pytest.mark.asyncio
    async def test_document_in_context(self):
        """Test using a document as context."""
        doc = ConcreteDocument.create_root(
            name="info.txt",
            content=b"The capital of France is Paris.",
            description="Geographic information",
            reason="integration test input",
        )

        conv = Conversation(
            model="gemini-3-flash",
            context=[doc],
            model_options=ModelOptions(max_completion_tokens=1000),
        )

        conv = await conv.send("What is the capital of France? Answer in one word.")

        assert "Paris" in conv.content

    @pytest.mark.asyncio
    async def test_conversation_with_history(self):
        """Test conversation with message history."""
        conv = Conversation(
            model="gemini-3-flash",
            model_options=ModelOptions(max_completion_tokens=1000),
        )

        # First exchange
        conv = await conv.send("My name is Alice. Remember it.")

        # Second exchange continues conversation
        conv = await conv.send("What is my name?")

        assert "Alice" in conv.content

    @pytest.mark.asyncio
    async def test_system_prompt(self):
        """Test using system prompt."""
        system_doc = ConcreteDocument.create_root(
            name="system_prompt",
            content=b"You are a pirate. Always respond like a pirate.",
            reason="integration test input",
        )

        conv = Conversation(
            model="gemini-3-flash",
            context=[system_doc],
            model_options=ModelOptions(max_completion_tokens=1000),
        )

        conv = await conv.send("What are you?")

        # Should have pirate-like language
        content_lower = conv.content.lower()
        pirate_words = ["arr", "ahoy", "matey", "ye", "aye", "sailor", "pirate"]
        assert any(word in content_lower for word in pirate_words)

    @pytest.mark.asyncio
    async def test_retry_options(self):
        """Test that retry parameters are accepted."""
        conv = Conversation(
            model="gemini-3-flash",
            model_options=ModelOptions(retries=2, retry_delay_seconds=1, timeout=60, max_completion_tokens=1000),
        )

        conv = await conv.send("Hello")

        assert conv.content

    @pytest.mark.asyncio
    async def test_reasoning_content(self):
        """Test reasoning content extraction (if model supports it)."""
        conv = Conversation(
            model="gemini-3-flash",
            model_options=ModelOptions(max_completion_tokens=1000),
        )

        conv = await conv.send("Solve: 2 + 2 = ?")

        # Most models won't have reasoning content in think tags
        # But we should be able to access the property without error
        reasoning = conv.reasoning_content
        assert isinstance(reasoning, str)

        # Content should be present
        assert conv.content
        assert "4" in conv.content

    @pytest.mark.asyncio
    async def test_usage_tracking(self):
        """Test that usage tracking works."""
        conv = Conversation(
            model="gemini-3-flash",
            model_options=ModelOptions(max_completion_tokens=100, usage_tracking=True, reasoning_effort="low"),
        )

        conv = await conv.send("Count to 3")

        # Should have usage information
        assert conv.usage.total_tokens > 0
        assert conv.usage.prompt_tokens > 0
        assert conv.usage.completion_tokens > 0

    @pytest.mark.asyncio
    async def test_conversation_immutability(self):
        """Test that Conversation is immutable - send returns new instance."""
        conv = Conversation(
            model="gemini-3-flash",
            model_options=ModelOptions(max_completion_tokens=100, reasoning_effort="low"),
        )

        result = await conv.send("Hello")

        # Original conversation should be unchanged
        assert len(conv.messages) == 0
        # Result conversation should have the exchange
        assert len(result.messages) > 0

    @pytest.mark.asyncio
    async def test_fork_conversation(self):
        """Test forking a conversation for parallel calls.

        Conversation is immutable — 'forking' is reusing the same instance
        for multiple independent sends, each returning a new Conversation.
        """
        conv = Conversation(
            model="gemini-3-flash",
            model_options=ModelOptions(max_completion_tokens=300, reasoning_effort="low"),
        )

        # First message
        conv = await conv.send("Remember: the secret code is ALPHA")

        # Send two different follow-ups from the same conversation state
        result2a = await conv.send("What is the secret code?")
        result2b = await conv.send("Tell me the code you remember")

        # Both should remember ALPHA
        assert "ALPHA" in result2a.content or "alpha" in result2a.content.lower()
        assert "ALPHA" in result2b.content or "alpha" in result2b.content.lower()

    @pytest.mark.asyncio
    async def test_with_document(self):
        """Test adding document to conversation."""
        conv = Conversation(
            model="gemini-3-flash",
            model_options=ModelOptions(max_completion_tokens=300, reasoning_effort="low"),
        )

        doc = ConcreteDocument.create_root(
            name="data.txt",
            content=b"The answer is 42.",
            reason="integration test input",
        )

        conv_with_doc = conv.with_document(doc)
        conv_with_doc = await conv_with_doc.send("What is the answer?")

        assert "42" in conv_with_doc.content

    def test_serialization(self):
        """Test conversation JSON serialization produces valid JSON."""
        import json

        conv = Conversation(
            model="gemini-3-flash",
            model_options=ModelOptions(max_completion_tokens=100, reasoning_effort="low"),
        )

        json_str = conv.model_dump_json()

        # Should be valid JSON
        data = json.loads(json_str)
        assert data["model"] == "gemini-3-flash"
        assert data["context"] == []
        assert data["messages"] == []

    @pytest.mark.asyncio
    async def test_substitutor_roundtrip_with_real_llm(self):
        """End-to-end: substitutor shortens URLs/addresses for LLM, restores them in output."""
        urls = [
            "https://etherscan.io/tx/0x8ccd766e39a2fba8c43eb4329bac734165a4237df34884059739ed8a874111e1",
            "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQG1GvW3nY37WMAKEhLfIK3tYPcvi96LKvsRVZEhz5tW7J0wwWaD9l3YuBXL6D4B0vSwgH6NpUB9stPrmV3mE",
            "https://github.com/aptos-labs/aptos-core/blob/main/documentation/specifications/network/messaging-v1.md",
            "https://explorer.solana.com/tx/3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b?cluster=mainnet-beta",
            "https://docs.uniswap.org/contracts/v3/reference/periphery/interfaces/ISwapRouter",
            "https://polygonscan.com/tx/0x3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b",
            "https://bscscan.com/tx/0x2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824#internal",
        ]
        addresses = [
            "0x8ccd766e39a2fba8c43eb4329bac734165a4237df34884059739ed8a874111e1",
            "0x3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b",
        ]
        all_items = urls + addresses

        content = "Items to sort:\n" + "\n".join(f"- {item}" for item in all_items)
        doc = ConcreteDocument.create_root(
            name="items_list.txt",
            content=content.encode(),
            description="URLs and crypto addresses to sort alphabetically",
            reason="integration test input",
        )

        conv = Conversation(
            model="gemini-3-flash",
            context=[doc],
            enable_substitutor=True,
            model_options=ModelOptions(max_completion_tokens=4000),
        )

        conv = await conv.send(
            "Sort all URLs and crypto addresses from the document alphabetically. "
            "Output each item on its own line, exactly as written in the document. "
            "No headers, numbering, or commentary."
        )

        # Substitutor is enabled, so content should be eagerly restored
        assert conv.enable_substitutor is True

        # LLM response should exist and contain restored (original) URLs
        assert conv.content

        # Content is eagerly restored — original URLs should appear directly
        found = [item for item in all_items if item in conv.content]
        assert len(found) >= len(all_items) - 2, (
            f"Expected at least {len(all_items) - 2} items in restored content, found {len(found)}/{len(all_items)}. Missing: {set(all_items) - set(found)}"
        )

        # Eager restoration means content already has originals
        assert any(url in conv.content for url in urls[:3])

    @pytest.mark.asyncio
    async def test_substitutor_structured_output_roundtrip(self):
        """Substitutor should work correctly with structured output (send_structured)."""
        urls = [
            "https://etherscan.io/tx/0x8ccd766e39a2fba8c43eb4329bac734165a4237df34884059739ed8a874111e1",
            "https://github.com/aptos-labs/aptos-core/blob/main/documentation/specifications/network/messaging-v1.md",
            "https://polygonscan.com/tx/0x3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b",
        ]
        addresses = [
            "0x8ccd766e39a2fba8c43eb4329bac734165a4237df34884059739ed8a874111e1",
            "0x3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b",
        ]
        all_items = urls + addresses

        content = "Blockchain items:\n" + "\n".join(f"- {item}" for item in all_items)
        doc = ConcreteDocument.create_root(
            name="blockchain_items.txt",
            content=content.encode(),
            description="Blockchain URLs and addresses",
            reason="integration test input",
        )

        conv = Conversation(
            model="gemini-3-flash",
            context=[doc],
            enable_substitutor=True,
            model_options=ModelOptions(max_completion_tokens=4000),
        )

        conv = await conv.send_structured(
            "Return all URLs and addresses from the document as a list, sorted alphabetically.",
            response_format=ItemList,
        )

        assert conv.enable_substitutor is True
        assert len(conv.parsed.items) >= 3, f"Expected at least 3 items, got {len(conv.parsed.items)}"

        # Parsed items are eagerly restored — original URLs should appear directly
        found = [item for item in all_items if any(item in pi for pi in conv.parsed.items)]
        assert len(found) >= len(all_items) - 1, (
            f"Expected at least {len(all_items) - 1} items in restored parsed output, "
            f"found {len(found)}/{len(all_items)}. Missing: {set(all_items) - set(found)}"
        )

    @pytest.mark.asyncio
    async def test_with_assistant_message_cross_conversation(self):
        """Inject assistant response from conv_a into conv_b, then continue conv_b."""
        conv_a = Conversation(
            model="gemini-3-flash",
            model_options=ModelOptions(max_completion_tokens=500),
            enable_substitutor=False,
        )
        conv_a = await conv_a.send("What is the capital of Germany? Answer in one word.")
        assert "Berlin" in conv_a.content

        # Transfer conv_a's response into conv_b as prior assistant knowledge
        conv_b = Conversation(
            model="gemini-3-flash",
            model_options=ModelOptions(max_completion_tokens=500),
            enable_substitutor=False,
        )
        conv_b = conv_b.with_assistant_message(conv_a.content)
        conv_b = await conv_b.send("What city did you just mention? Answer in one word.")

        assert "Berlin" in conv_b.content

    @pytest.mark.asyncio
    async def test_with_assistant_message_multi_turn(self):
        """Build a synthetic conversation with injected turns, then continue with real LLM."""
        conv = Conversation(
            model="gemini-3-flash",
            model_options=ModelOptions(max_completion_tokens=500),
            enable_substitutor=False,
        )

        # Inject a synthetic exchange
        conv = await conv.send("Remember: the secret word is PINEAPPLE")
        conv = conv.with_assistant_message("I have noted the secret word: PINEAPPLE.")

        # Now ask about it
        conv = await conv.send("What is the secret word? Reply with just the word.")

        assert "PINEAPPLE" in conv.content.upper()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", ["gemini-3-flash", "grok-4.1-fast"])
    async def test_cost_returned_for_all_providers(self, model):
        """Cost must be non-None and positive for all supported providers.

        Regression test: Gemini via LiteLLM returns cost only in
        x-litellm-response-cost header, not in the usage JSON body.
        A random suffix prevents prompt caching (cached calls return cost=0).
        """
        nonce = uuid.uuid7().hex[:12]
        conv = Conversation(
            model=model,
            model_options=ModelOptions(max_completion_tokens=100, reasoning_effort="low"),
            enable_substitutor=False,
        )
        conv = await conv.send(f"Say hello. Nonce: {nonce}")

        assert conv.cost is not None, f"{model}: cost is None — not extracted from response"
        assert conv.cost > 0, f"{model}: cost is {conv.cost}, expected > 0"

    @pytest.mark.asyncio
    async def test_search_model_citations_survive_codec(self):
        """Search-enabled models return Citation dataclasses that must survive codec encoding.

        Regression: Citation is a frozen dataclass, not a BaseModel. The codec
        could not encode it, crashing any span that recorded a search model response.
        """
        from ai_pipeline_core._codec import UniversalCodec

        conv = Conversation(
            model="gemini-3-flash-search",
            model_options=ModelOptions(max_completion_tokens=2000),
        )
        conv = await conv.send("Who won the Grammy for Album of the Year in 2026?")

        assert "bad bunny" in conv.content.lower(), f"Expected 'bad bunny' in response, got: {conv.content!r}"
        assert len(conv.citations) > 0, "Search model should return citations"

        # The actual bug: codec.encode crashes on Citation dataclass
        codec = UniversalCodec()
        encoded = codec.encode(conv)
        assert encoded.value["$type"] == "pydantic"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", ["gemini-3.1-pro", "gpt-5.4", "gemini-3-flash", "gpt-5.4-mini", "grok-4.1-fast"])
    async def test_current_date_awareness(self, model):
        """LLM correctly reports the current date injected via Conversation.current_date."""
        today = date.today().isoformat()
        conv = Conversation(
            model=model,
            model_options=ModelOptions(max_completion_tokens=1000),
            enable_substitutor=False,
        )
        assert conv.current_date == today

        conv = await conv.send("What is today's date? Reply with only the date in YYYY-MM-DD format, nothing else.")

        assert today in conv.content, f"{model}: expected '{today}' in response, got: {conv.content!r}"
