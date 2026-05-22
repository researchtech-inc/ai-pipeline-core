"""Integration tests for replay deployment pinning."""

from typing import Any

import pytest

from ai_pipeline_core._llm_core.model_response import ModelResponse
from ai_pipeline_core.database import SpanKind, SpanRecord
from ai_pipeline_core.llm import AIModel, Conversation
from ai_pipeline_core.replay import ExperimentOverrides, execute_span, experiment_span
from ai_pipeline_core.settings import settings
from tests.support.helpers import RecordingSpanDatabase, TransportSpy, last_model_response, span_meta

HAS_API_KEYS = bool(settings.openai_api_key and settings.openai_base_url)

pytestmark = [
    pytest.mark.skipif(not HAS_API_KEYS, reason="LLM API credentials not configured"),
]

_REPLAY_TARGET = "function:ai_pipeline_core.llm._engine:_replay_llm_round"


async def _record_replay_source(
    *,
    nonce: str,
    database: RecordingSpanDatabase,
    recording_context: Any,
    model: AIModel,
) -> tuple[SpanRecord, ModelResponse[Any]]:
    """Record one live LLM round suitable for replay tests."""
    with recording_context(database):
        conv = await Conversation(model=model, enable_substitutor=False).send(
            f"Return a concise replay source answer. Nonce: {nonce}",
            purpose=f"replay-source-{nonce}",
        )
    return database.completed_spans_by_kind(SpanKind.LLM_ROUND)[0], last_model_response(conv)


class TestLLMReplayPinning:
    """Verify replay pins recorded deployments unless experiment overrides replace the model."""

    @pytest.mark.asyncio
    async def test_replay_llm_round_pins_recorded_deployment(
        self,
        nonce: str,
        recording_db: RecordingSpanDatabase,
        recording_context: Any,
        transport_spy: TransportSpy,
        cost_budget: Any,
        default_test_model: AIModel,
    ) -> None:
        """Replaying an LLM round pins the recorded AIPL deployment id."""
        source_span, source_response = await _record_replay_source(
            nonce=nonce, database=recording_db, recording_context=recording_context, model=default_test_model
        )
        cost_budget.add(source_response)
        original_deployment_id = span_meta(source_span)["aipl"]["deployment_id"]
        replayed = await execute_span(source_span.span_id, source_db=recording_db, sink_db=RecordingSpanDatabase())

        cost_budget.add(replayed)
        assert source_span.target == _REPLAY_TARGET
        assert original_deployment_id
        assert isinstance(replayed, ModelResponse)
        assert replayed.transport.aipl.deployment_id == original_deployment_id
        assert (
            transport_spy.recorded_calls[-1].api_kwargs["metadata"]["aipl_force_deployment_id"]
            == original_deployment_id
        )
        assert replayed.transport.model_chain.requested == source_response.transport.model_chain.requested
        assert replayed.transport.model_chain.active == source_response.transport.model_chain.active

    @pytest.mark.asyncio
    async def test_replay_with_model_override_skips_recorded_deployment_pin(
        self,
        nonce: str,
        recording_db: RecordingSpanDatabase,
        recording_context: Any,
        transport_spy: TransportSpy,
        cost_budget: Any,
        default_test_model: AIModel,
        alternate_test_model: AIModel,
    ) -> None:
        """Experiment model overrides skip force-deployment replay pinning."""
        source_span, source_response = await _record_replay_source(
            nonce=nonce, database=recording_db, recording_context=recording_context, model=default_test_model
        )
        cost_budget.add(source_response)
        override_model = alternate_test_model
        replay_db = RecordingSpanDatabase()

        result = await experiment_span(
            source_span.span_id,
            source_db=recording_db,
            sink_db=replay_db,
            overrides=ExperimentOverrides(model=override_model),
        )

        cost_budget.add(result.result)
        assert isinstance(result.result, ModelResponse)
        assert "aipl_force_deployment_id" not in transport_spy.recorded_calls[-1].api_kwargs["metadata"]
        assert result.result.transport.model_chain.requested == override_model.name
