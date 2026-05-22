"""Integration tests for substituted URLs in tool-call arguments."""

from typing import Any

import pytest
from pydantic import BaseModel, Field

from ai_pipeline_core.database import SpanKind
from ai_pipeline_core.llm import AIModel, Conversation, Tool
from ai_pipeline_core.settings import settings
from tests.support.helpers import TransportSpy, message_payload_text, span_input

HAS_API_KEYS = bool(settings.openai_api_key and settings.openai_base_url)

pytestmark = [
    pytest.mark.skipif(not HAS_API_KEYS, reason="LLM API credentials not configured"),
]


class UrlReceiverTool(Tool):
    """Receive a URL and echo the exact argument value."""

    class Input(BaseModel):
        """Input for the URL receiver tool."""

        url: str = Field(description="URL received by the tool.")

    class Output(BaseModel):
        """Output from the URL receiver tool."""

        echoed_url: str = Field(description="Exact URL received by the tool.")

    async def run(self, input: Input) -> Output:
        """Echo the received URL."""
        return self.Output(echoed_url=input.url)


class TestLLMSubstitutorToolArgs:
    """Pin documented shortened-URL behavior for tool call arguments."""

    @pytest.mark.asyncio
    async def test_substituted_url_reaches_tool_in_shortened_form(
        self,
        nonce: str,
        transport_spy: TransportSpy,
        recording_db: Any,
        recording_context: Any,
        cost_budget: Any,
        default_test_model: AIModel,
    ) -> None:
        """The tool receives the model-emitted shortened URL form."""
        long_url = f"https://example.com/research/{nonce}/alpha-beta-gamma-delta-epsilon-zeta-eta-theta-iota-kappa-lambda-mu/report-one"
        with recording_context(recording_db):
            conv = await Conversation(
                model=default_test_model,
                enable_substitutor=True,
            ).send(
                f"Call url_receiver_tool with this URL exactly as shown: {long_url}. Nonce: {nonce}",
                tools=[UrlReceiverTool()],
                tool_choice="required",
                max_tool_rounds=1,
                purpose=f"substitutor-tool-args-{nonce}",
            )

        cost_budget.add(conv)
        tool_span = recording_db.completed_spans_by_kind(SpanKind.TOOL_CALL)[0]
        assert conv.tool_call_records
        record_url = conv.tool_call_records[0].input.url
        span_url = span_input(tool_span)["input"]["url"]
        model_payload = message_payload_text(transport_spy.recorded_calls[0].messages)

        assert "..." in record_url
        assert long_url not in record_url
        assert span_url == record_url
        assert long_url not in model_payload
        assert record_url in model_payload
