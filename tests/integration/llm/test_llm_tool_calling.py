"""Integration tests for live LLM tool calling."""

from typing import Any

import pytest
from pydantic import BaseModel, Field

from ai_pipeline_core.llm import AIModel, Conversation, Tool
from ai_pipeline_core.settings import settings

HAS_API_KEYS = bool(settings.openai_api_key and settings.openai_base_url)

pytestmark = [
    pytest.mark.skipif(not HAS_API_KEYS, reason="LLM API credentials not configured"),
]


class CityInfo(BaseModel):
    """Structured city result after tool use."""

    city: str = Field(description="Capital city name.")
    country: str = Field(description="Country name.")


class GetCapital(Tool):
    """Get the capital city of a country."""

    class Input(BaseModel):
        country: str = Field(description="Country name.")

    class Output(BaseModel):
        answer: str

    async def run(self, input: Input) -> Output:
        capitals = {
            "france": "Paris",
            "germany": "Berlin",
            "japan": "Tokyo",
            "brazil": "Brasilia",
        }
        city = capitals.get(input.country.lower())
        if city is None:
            return self.Output(answer=f"Unknown country: {input.country}")
        return self.Output(answer=f"The capital of {input.country} is {city}.")


class FailingTool(Tool):
    """A tool that always fails."""

    class Input(BaseModel):
        reason: str = Field(description="Why the tool should fail.")

    class Output(BaseModel):
        result: str

    async def run(self, input: Input) -> Output:
        raise RuntimeError(f"Intentional failure: {input.reason}")


class WeatherLookup(Tool):
    """Look up weather for a city."""

    class Input(BaseModel):
        city: str = Field(description="City to look up.")

    class Output(BaseModel):
        weather: str

    async def run(self, input: Input) -> Output:
        return self.Output(weather=f"The weather in {input.city} is sunny, 22C.")


class CounterAdd(Tool):
    """Increment a counter and return the new value."""

    class Input(BaseModel):
        """Empty tool input."""

    class Output(BaseModel):
        value: int = Field(description="Counter value after increment.")

    def __init__(self) -> None:
        self.counter = 0

    async def run(self, input: Input) -> Output:
        _ = input
        self.counter += 1
        return self.Output(value=self.counter)


class TestLLMToolCalling:
    """Recover live tool-calling coverage."""

    @pytest.mark.asyncio
    async def test_single_tool_call(
        self,
        default_test_model: AIModel,
        cost_budget: Any,
    ) -> None:
        """The model can call a single tool and answer with the result."""
        conv = await Conversation(model=default_test_model, enable_substitutor=False).send(
            "What is the capital of France? Use the get_capital tool.",
            tools=[GetCapital()],
            purpose="tool-calling-single-tool",
        )

        cost_budget.add(conv)
        assert "paris" in conv.content.lower()
        assert conv.tool_call_records
        assert conv.tool_call_records[0].tool is GetCapital

    @pytest.mark.asyncio
    async def test_multiple_tools_available(
        self,
        default_test_model: AIModel,
        cost_budget: Any,
    ) -> None:
        """The model selects the appropriate tool from multiple options."""
        conv = await Conversation(model=default_test_model, enable_substitutor=False).send(
            "What is the capital of Japan? Use the appropriate tool.",
            tools=[WeatherLookup(), GetCapital()],
            purpose="tool-calling-multiple-tools",
        )

        cost_budget.add(conv)
        assert "tokyo" in conv.content.lower()
        assert any(record.tool is GetCapital for record in conv.tool_call_records)

    @pytest.mark.asyncio
    async def test_tool_error_recovery(
        self,
        default_test_model: AIModel,
        cost_budget: Any,
    ) -> None:
        """Tool failures are surfaced back into a final response."""
        conv = await Conversation(model=default_test_model, enable_substitutor=False).send(
            "Use the failing_tool with reason 'test', then tell me what happened.",
            tools=[FailingTool()],
            tool_choice="required",
            max_tool_rounds=2,
            purpose="tool-calling-error-recovery",
        )

        cost_budget.add(conv)
        assert conv.content
        assert any(record.tool is FailingTool for record in conv.tool_call_records)
        assert len(conv.messages) > 1

    @pytest.mark.asyncio
    async def test_tool_choice_none(
        self,
        default_test_model: AIModel,
        cost_budget: Any,
    ) -> None:
        """tool_choice='none' suppresses tool use."""
        conv = await Conversation(model=default_test_model, enable_substitutor=False).send(
            "What is the capital of Germany? Answer directly.",
            tools=[GetCapital()],
            tool_choice="none",
            purpose="tool-calling-choice-none",
        )

        cost_budget.add(conv)
        assert conv.content
        assert len(conv.tool_call_records) == 0

    @pytest.mark.asyncio
    async def test_structured_output_with_tools(
        self,
        default_test_model: AIModel,
        cost_budget: Any,
    ) -> None:
        """Structured output still works after a tool loop."""
        conv = await Conversation(model=default_test_model, enable_substitutor=False).send_structured(
            "Look up the capital of France using the tool, then return JSON with city and country.",
            response_format=CityInfo,
            tools=[GetCapital()],
            purpose="tool-calling-structured-output",
        )

        cost_budget.add(conv)
        assert conv.parsed.city.lower() == "paris"
        assert conv.parsed.country.lower() == "france"

    @pytest.mark.asyncio
    async def test_tool_choice_required(
        self,
        default_test_model: AIModel,
        cost_budget: Any,
    ) -> None:
        """tool_choice='required' produces at least one tool call."""
        conv = await Conversation(model=default_test_model, enable_substitutor=False).send(
            "Use the weather_lookup tool to tell me the weather in Paris.",
            tools=[WeatherLookup()],
            tool_choice="required",
            purpose="tool-calling-choice-required",
        )

        cost_budget.add(conv)
        assert conv.content
        assert conv.tool_call_records
        assert conv.tool_call_records[0].tool is WeatherLookup

    @pytest.mark.asyncio
    async def test_conversation_continues_after_tools(
        self,
        default_test_model: AIModel,
        cost_budget: Any,
    ) -> None:
        """Conversation history survives a prior tool round."""
        conv = await Conversation(model=default_test_model, enable_substitutor=False).send(
            "What is the capital of France? Use the get_capital tool.",
            tools=[GetCapital()],
            purpose="tool-calling-continuation-1",
        )
        cost_budget.add(conv)
        assert "paris" in conv.content.lower()

        conv = await conv.send(
            "What country was that capital in?",
            purpose="tool-calling-continuation-2",
        )
        cost_budget.add(conv)

        assert "france" in conv.content.lower()

    @pytest.mark.asyncio
    async def test_forced_final_produces_real_response_not_synthetic(
        self,
        test_model_choice: AIModel,
        cost_budget: Any,
    ) -> None:
        """Forced final synthesis returns a real answer and keeps steering transient."""
        tool = CounterAdd()
        conv = await Conversation(model=test_model_choice, enable_substitutor=False).send(
            "Use the counter_add tool to increment the counter. Keep going until the counter reaches 10.",
            tools=[tool],
            tool_choice="required",
            max_tool_rounds=1,
            purpose=f"tool-calling-forced-final-{test_model_choice.name}",
        )

        cost_budget.add(conv)
        assert tool.counter >= 1
        assert conv.tool_call_records
        assert conv.content
        assert "[Final synthesis failed" not in conv.content
        assert not any("no more tools" in getattr(message, "text", "").lower() for message in conv.messages)

    @pytest.mark.asyncio
    async def test_follow_up_tool_call_works_after_forced_final(
        self,
        test_model_choice: AIModel,
        cost_budget: Any,
    ) -> None:
        """A follow-up send with tools still works after forced-final steering."""
        conv = await Conversation(model=test_model_choice, enable_substitutor=False).send(
            "Use the get_capital tool to look up the capital of France.",
            tools=[GetCapital()],
            tool_choice="required",
            max_tool_rounds=1,
            purpose=f"tool-calling-followup-1-{test_model_choice.name}",
        )
        cost_budget.add(conv)
        assert "[Final synthesis failed" not in conv.content

        conv = await conv.send(
            "Now use the get_capital tool to look up the capital of Japan.",
            tools=[GetCapital()],
            purpose=f"tool-calling-followup-2-{test_model_choice.name}",
        )
        cost_budget.add(conv)

        assert "tokyo" in conv.content.lower()
        assert conv.tool_call_records
