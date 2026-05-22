"""Integration tests for PromptSpec output_structure extraction."""

from typing import Any

import pytest
from pydantic import Field

from ai_pipeline_core.llm import AIModel, Conversation
from ai_pipeline_core.prompt_compiler.components import Role
from ai_pipeline_core.prompt_compiler.spec import PromptSpec
from ai_pipeline_core.settings import settings
from tests.support.helpers import TransportSpy, last_model_response, span_meta

HAS_API_KEYS = bool(settings.openai_api_key and settings.openai_base_url)

pytestmark = [
    pytest.mark.skipif(not HAS_API_KEYS, reason="LLM API credentials not configured"),
]


class OutputStructureRole(Role):
    """Role for output-structure integration tests."""

    text = "concise geography analyst"


class CityResultSpec(PromptSpec[str]):
    """Return a city name inside the framework result wrapper."""

    input_documents = ()
    role = OutputStructureRole
    task = "Return exactly the provided nonce prefixed by PARIS-. Do not add any other words."
    output_structure = "## Result\nA single literal value."
    nonce: str = Field(description="Nonce to include in the answer.")


class TestPromptSpecOutputStructure:
    """Verify PromptSpec output_structure request and response behavior."""

    @pytest.mark.asyncio
    async def test_output_structure_extracts_result_tags(
        self,
        nonce: str,
        transport_spy: TransportSpy,
        recording_db: Any,
        recording_context: Any,
        cost_budget: Any,
        default_test_model: AIModel,
    ) -> None:
        """send_spec strips result tags and sends the stop sequence."""
        spec = CityResultSpec(nonce=nonce)
        with recording_context(recording_db):
            conv = await Conversation(
                model=default_test_model,
                enable_substitutor=False,
            ).send_spec(spec, purpose=f"promptspec-output-structure-{nonce}")

        cost_budget.add(conv)
        response = last_model_response(conv)
        llm_round = recording_db.completed_spans_by_kind("llm_round")[0]

        assert conv.extract_result_tags is True
        assert "<result>" not in conv.content
        assert "</result>" not in conv.content
        assert "PARIS" in conv.content.upper()
        assert nonce in conv.content
        assert response.content
        assert "</result>" in transport_spy.recorded_calls[0].api_kwargs["stop"]
        assert span_meta(llm_round)["response_format_path"] == ""
