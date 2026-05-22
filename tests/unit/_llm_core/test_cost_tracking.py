"""Cost propagation from streamed usage into typed transport metadata."""

from types import SimpleNamespace
from typing import Any

from ai_pipeline_core._llm_core._aipl import AIPLResponseHeaders
from ai_pipeline_core._llm_core._response_builder import _build_model_response
from ai_pipeline_core._llm_core.model_response import StreamCompletion, TimingData
from ai_pipeline_core._llm_core.request import AttemptRequest, LLMRequest
from ai_pipeline_core._llm_core.types import CoreMessage, Role
from tests.support.model_catalog import DEFAULT_TEST_MODEL


def _attempt() -> AttemptRequest:
    request = LLMRequest(model=DEFAULT_TEST_MODEL, messages=(CoreMessage(role=Role.USER, content="hi"),))
    return AttemptRequest(call=request, model=request.model, attempt_index=0, call_id="call_1")


def _completion(*, usage: dict[str, Any], header_cost: float | None) -> StreamCompletion:
    message = SimpleNamespace(
        content="ok",
        reasoning_content=None,
        annotations=[],
        tool_calls=[],
        provider_specific_fields=None,
        thinking_blocks=None,
    )
    response = SimpleNamespace(
        id="resp-1", choices=(SimpleNamespace(message=message, finish_reason="stop"),), usage=None
    )
    return StreamCompletion(
        response=response,
        usage=usage,
        raw_headers={},
        aipl_headers=AIPLResponseHeaders(response_cost=header_cost),
        timing=TimingData(started_at=0.0, first_token_at=0.1, finished_at=0.5),
        final_tps=8.0,
    )


def test_usage_cost_populates_response_and_transport_when_header_missing() -> None:
    response = _build_model_response(
        _completion(
            usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3, "cost": 0.123}, header_cost=None
        ),
        _attempt(),
        prompt_cache_key=None,
    )

    assert response.cost == 0.123
    assert response.transport.litellm.response_cost == 0.123


def test_header_cost_is_fallback_when_usage_cost_missing() -> None:
    response = _build_model_response(
        _completion(usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}, header_cost=0.045),
        _attempt(),
        prompt_cache_key=None,
    )

    assert response.cost == 0.045
    assert response.transport.litellm.response_cost == 0.045


def test_usage_cost_wins_over_header_cost() -> None:
    response = _build_model_response(
        _completion(
            usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3, "cost": 0.222}, header_cost=0.111
        ),
        _attempt(),
        prompt_cache_key=None,
    )

    assert response.cost == 0.222
    assert response.transport.litellm.response_cost == 0.222
