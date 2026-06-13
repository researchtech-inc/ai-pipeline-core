"""Integration coverage for LLM request attribution through Conversation."""

from typing import Any

import pytest

from ai_pipeline_core.llm import AIModel, Conversation, ModelOptions
from ai_pipeline_core.settings import settings
from tests.support.helpers import RecordingSpanDatabase, span_input

HAS_API_KEYS = bool(settings.openai_api_key and settings.openai_base_url)

pytestmark = [
    pytest.mark.skipif(not HAS_API_KEYS, reason="LLM API credentials not configured"),
]


class TestLLMRequestAttribution:
    """Live checks for request attribution recorded by the engine."""

    @pytest.mark.asyncio
    async def test_conversation_records_tenant_attribution_on_llm_request(
        self,
        default_test_model: AIModel,
        recording_db: RecordingSpanDatabase,
        recording_context: Any,
        cost_budget: Any,
        nonce: str,
    ) -> None:
        """A live Conversation call carries execution-context tenant fields into the LLM request."""
        run_id = f"tenant-run-{nonce}"
        with recording_context(recording_db, run_id):
            conv = await Conversation(
                model=default_test_model,
                enable_substitutor=False,
                model_options=ModelOptions(reasoning_effort="low"),
            ).send(
                "Reply with one short sentence saying attribution test ok.",
                purpose=f"tenant-purpose-{nonce}",
            )

        cost_budget.add(conv)
        llm_round = recording_db.completed_spans_by_kind("llm_round")[0]
        llm_request = span_input(llm_round)["llm_request"]["data"]
        tenant = llm_request["tenant"]["data"]

        assert conv.content
        assert tenant["run_id"] == run_id
        assert tenant["workload_stage"] == "IntegrationFlow"
