"""AI-doc examples for Conversation: tools, caching, forking, and document serialization."""

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel, Field

from ai_pipeline_core._llm_core.model_response import ModelResponse
from ai_pipeline_core._llm_core import LLMRequest
from ai_pipeline_core._llm_core.types import CoreMessage, RawToolCall, Role, TokenUsage
from ai_pipeline_core.llm._request_assembly import to_core_messages
from ai_pipeline_core.llm.conversation import Conversation
from ai_pipeline_core.llm.tools import Tool
from tests.support.helpers import ConcreteDocument, create_test_model_response
from tests.support.model_catalog import DEFAULT_TEST_MODEL


@pytest.mark.ai_docs
@pytest.mark.asyncio
async def test_warmup_then_parallel_forks(monkeypatch):
    """Warmup populates cache, forks share the warmed prefix including warmup response."""
    captured_calls: list[list[CoreMessage]] = []

    async def fake_generate(request: LLMRequest) -> Any:
        messages = list(request.messages)
        captured_calls.append(messages)
        for msg in reversed(messages):
            if msg.role == Role.USER and isinstance(msg.content, str):
                return create_test_model_response(content=f"Answer: {msg.content}")
        return create_test_model_response(content="ok")

    monkeypatch.setattr("ai_pipeline_core.llm._engine.generate", fake_generate)

    shared_doc = ConcreteDocument(name="shared_context.md", content=b"Shared context for all forks.")
    base = Conversation(model=DEFAULT_TEST_MODEL, enable_substitutor=False).with_context(shared_doc)
    warmed = await base.send("Acknowledge the context.")
    fork_a, fork_b = await asyncio.gather(
        warmed.send("Question A"),
        warmed.send("Question B"),
    )

    assert warmed.content == "Answer: Acknowledge the context."
    assert fork_a.content == "Answer: Question A"
    assert fork_b.content == "Answer: Question B"

    # Fork calls include the warmup response as an assistant message in their history
    fork_calls = captured_calls[1:]
    assert len(fork_calls) == 2
    for call in fork_calls:
        assistant_msgs = [m for m in call if m.role == Role.ASSISTANT]
        assert any(
            "Answer: Acknowledge the context." in m.content for m in assistant_msgs if isinstance(m.content, str)
        )


@pytest.mark.ai_docs
@pytest.mark.asyncio
async def test_context_is_cached_prefix_messages_are_dynamic_suffix(monkeypatch):
    """with_context() adds to cacheable prefix (context_count); with_document() adds to dynamic suffix."""
    captured_context_counts: list[int] = []

    async def fake_generate(request: LLMRequest) -> Any:
        captured_context_counts.append(request.cache.context_count)
        return create_test_model_response(content="ok")

    monkeypatch.setattr("ai_pipeline_core.llm._engine.generate", fake_generate)

    context_doc = ConcreteDocument(name="cached.md", content=b"Cached context")
    message_doc = ConcreteDocument(name="dynamic.md", content=b"Dynamic data")
    conv = Conversation(model=DEFAULT_TEST_MODEL, enable_substitutor=False, include_date=False)
    conv = conv.with_context(context_doc)
    conv = conv.with_document(message_doc)
    await conv.send("Question")

    # context_count tells the provider how many leading messages form the cached prefix
    assert captured_context_counts == [1]


@pytest.mark.ai_docs
def test_document_wrapped_in_xml_tags():
    """Documents sent to the LLM are wrapped in <document> XML tags with id, name, description, content."""
    doc = ConcreteDocument(
        name="report.md",
        content=b"Final findings here.",
        description="Research report",
    )
    conv = Conversation(model=DEFAULT_TEST_MODEL, enable_substitutor=False)
    core_messages = to_core_messages((doc,), conv.model)

    assert len(core_messages) == 1
    assert core_messages[0].role == Role.USER
    content = core_messages[0].content
    assert isinstance(content, str)
    assert "<document>" in content
    assert f"<id>{doc.id}</id>" in content
    assert "<name>report.md</name>" in content
    assert "<description>Research report</description>" in content
    assert "<content>" in content
    assert "Final findings here." in content
    assert "</document>" in content


class GetWeather(Tool):
    """Get current weather for a city."""

    class Input(BaseModel):
        city: str = Field(description="City name")

    class Output(BaseModel):
        weather: str

    async def run(self, input: Input) -> Output:
        return self.Output(weather=f"Sunny, 22°C in {input.city}")


@pytest.mark.ai_docs
@pytest.mark.asyncio
async def test_send_with_tools_auto_loop(monkeypatch):
    """Tools enable the LLM to call functions.

    Conversation.send() auto-loops: call LLM → execute tools → re-send results until LLM
    produces a final answer.
    """

    # Mock LLM: first call requests tool use, second returns final answer
    call_count = 0

    async def fake_generate(request: LLMRequest) -> Any:
        _ = request
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # LLM decides to call the get_weather tool
            return ModelResponse(
                content="Let me check the weather.",
                parsed="Let me check the weather.",
                model="test-model",
                usage=TokenUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
                tool_calls=(RawToolCall(id="call_1", function_name="get_weather", arguments='{"city": "Paris"}'),),
            )
        # LLM uses tool result to produce final answer
        return create_test_model_response(content="It's sunny and 22°C in Paris!")

    monkeypatch.setattr("ai_pipeline_core.llm._engine.generate", fake_generate)

    # 3. Send with tools — auto-loop handles tool execution transparently
    conv = Conversation(model=DEFAULT_TEST_MODEL, enable_substitutor=False)
    result = await conv.send("What's the weather in Paris?", tools=[GetWeather()])

    # 4. Final response text from the LLM
    assert result.content == "It's sunny and 22°C in Paris!"

    # 5. Tool call records track every tool invocation across all rounds
    assert len(result.tool_call_records) == 1
    record = result.tool_call_records[0]
    assert record.tool is GetWeather
    assert record.round == 1
    assert record.output.data is not None
    assert record.output.data.weather == "Sunny, 22°C in Paris"
