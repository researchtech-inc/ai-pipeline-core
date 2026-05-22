"""Integration tests for live URL substitution and restoration."""

from typing import Any

import pytest
from pydantic import BaseModel

from ai_pipeline_core.llm import AIModel, Conversation
from ai_pipeline_core.settings import settings
from tests.support.helpers import ConcreteDocument

HAS_API_KEYS = bool(settings.openai_api_key and settings.openai_base_url)

pytestmark = [
    pytest.mark.skipif(not HAS_API_KEYS, reason="LLM API credentials not configured"),
]


class ItemList(BaseModel):
    """Structured list response for substituted items."""

    items: list[str]


class TestLLMSubstitutor:
    """Recover live substitutor round-trip coverage."""

    @pytest.mark.asyncio
    async def test_substitutor_roundtrip_with_real_llm(
        self,
        default_test_model: AIModel,
        cost_budget: Any,
    ) -> None:
        """Substituted URLs are restored in plain-text output."""
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
        doc = ConcreteDocument.create_root(
            name="items_list.txt",
            content="Items to sort:\n" + "\n".join(f"- {item}" for item in all_items),
            description="URLs and crypto addresses to sort alphabetically",
            reason="integration test input",
        )

        conv = await Conversation(
            model=default_test_model,
            context=(doc,),
            enable_substitutor=True,
        ).send(
            "Sort all URLs and crypto addresses from the document alphabetically. "
            "Output each item on its own line, exactly as written in the document. "
            "No headers, numbering, or commentary.",
            purpose="llm-substitutor-roundtrip",
        )

        cost_budget.add(conv)
        found = [item for item in all_items if item in conv.content]

        assert conv.enable_substitutor is True
        assert conv.content
        assert len(found) >= len(all_items) - 2
        assert any(url in conv.content for url in urls[:3])

    @pytest.mark.asyncio
    async def test_substitutor_structured_output_roundtrip(
        self,
        default_test_model: AIModel,
        cost_budget: Any,
    ) -> None:
        """Substituted URLs are restored in structured list output."""
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
        doc = ConcreteDocument.create_root(
            name="blockchain_items.txt",
            content="Blockchain items:\n" + "\n".join(f"- {item}" for item in all_items),
            description="Blockchain URLs and addresses",
            reason="integration test input",
        )

        conv = await Conversation(
            model=default_test_model,
            context=(doc,),
            enable_substitutor=True,
        ).send_structured(
            "Return all URLs and addresses from the document as a list, sorted alphabetically.",
            response_format=ItemList,
            purpose="llm-substitutor-structured-roundtrip",
        )

        cost_budget.add(conv)
        found = [item for item in all_items if any(item in parsed_item for parsed_item in conv.parsed.items)]

        assert conv.enable_substitutor is True
        assert len(conv.parsed.items) >= 3
        assert len(found) >= len(all_items) - 1
