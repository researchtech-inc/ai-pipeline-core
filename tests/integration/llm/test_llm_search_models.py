"""Integration tests for search-enabled models (e2e through the live proxy).

These tests cover the search-models pipeline end-to-end:
* The LiteLLM proxy's Responses→Chat translator forwards built-in web-search
  lifecycle events as synthetic ``delta.tool_calls``.
* The framework's streaming watchdog tolerates the long upstream search
  phases via its tool-call-aware inactivity gate (relaxed to 120s while a
  tool call is in progress, snaps back to 30s on text content).
* The response builder still parses structured output even when synthetic
  ``web_search`` tool_calls are present (they are not user-declared, so the
  response is not in tool-execution mode).

Lightweight smoke prompts are used so the suite completes in the integration
lane budget. Deeper deep-research regressions live in the bench scripts.
"""

from datetime import date
from typing import Any

import pytest
from pydantic import BaseModel

from ai_pipeline_core.llm import AIModel, Conversation, ModelOptions
from ai_pipeline_core.settings import settings

HAS_API_KEYS = bool(settings.openai_api_key and settings.openai_base_url)

pytestmark = [
    pytest.mark.skipif(not HAS_API_KEYS, reason="LLM API credentials not configured"),
]


class WeatherReport(BaseModel):
    """Structured-output target for the search-model JSON regression test."""

    location: str
    summary: str


class TestSearchModelStreaming:
    """End-to-end smoke checks for search-enabled deployments."""

    @pytest.mark.asyncio
    @pytest.mark.timeout(420)
    async def test_gpt_search_returns_content_without_watchdog_kill(
        self,
        gpt_nano_search_test_model: AIModel,
        cost_budget: Any,
    ) -> None:
        """OpenAI search variant streams to completion under the framework's
        tool-call-aware watchdog. Search lifecycle deltas land within seconds
        of stream open and the relaxed inactivity gate covers the long
        upstream search phases.
        """
        today = date.today().isoformat()
        conv = await Conversation(
            model=gpt_nano_search_test_model,
            enable_substitutor=False,
            model_options=ModelOptions(reasoning_effort="low"),
        ).send(
            f"Search the web for the current weather in London. Today is {today}. "
            "Reply with one short sentence summarizing the result, then a list of source URLs.",
            purpose="search-models-smoke-gpt",
        )

        cost_budget.add(conv)
        assert conv.content, "search model returned empty content"

    @pytest.mark.asyncio
    @pytest.mark.timeout(420)
    async def test_gpt_search_with_structured_output_parses_json(
        self,
        gpt_nano_search_test_model: AIModel,
        cost_budget: Any,
    ) -> None:
        """Structured output works on search models — synthetic ``web_search``
        tool_calls do NOT block JSON parsing.

        Regression for the response-builder gate fix: the gate now keys on
        "tool_calls AND user-declared schemas" rather than "tool_calls"
        alone. Without the fix, structured-output requests against search
        models silently returned the raw text in ``parsed`` instead of the
        validated Pydantic instance.
        """
        conv = await Conversation(
            model=gpt_nano_search_test_model,
            enable_substitutor=False,
            model_options=ModelOptions(reasoning_effort="low"),
        ).send_structured(
            "Search the web for the current weather in Tokyo. Reply with a JSON object: "
            '{"location": "Tokyo", "summary": <one short sentence>}',
            response_format=WeatherReport,
            purpose="search-models-structured-gpt",
        )

        cost_budget.add(conv)
        assert isinstance(conv.parsed, WeatherReport)
        assert conv.parsed.location.lower().startswith("tok")
        assert conv.parsed.summary

    @pytest.mark.asyncio
    @pytest.mark.timeout(420)
    async def test_gemini_search_returns_content_without_watchdog_kill(
        self,
        gemini_flash_search_test_model: AIModel,
        cost_budget: Any,
    ) -> None:
        """Gemini's search variant streams text via ``delta.content`` directly
        (no synthetic tool_calls needed on its wire path). This test guards
        the cross-provider invariant: search workloads complete under the
        same Conversation API regardless of which provider's wire shape the
        proxy surfaces.
        """
        today = date.today().isoformat()
        conv = await Conversation(
            model=gemini_flash_search_test_model,
            enable_substitutor=False,
        ).send(
            f"Search for the population of Iceland. Today is {today}. Reply with one short sentence.",
            purpose="search-models-smoke-gemini",
        )

        cost_budget.add(conv)
        assert conv.content
