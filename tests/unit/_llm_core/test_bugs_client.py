"""Bug-proving tests for LLM response normalization."""

from types import SimpleNamespace
from typing import Any

import pytest

from ai_pipeline_core._llm_core._aipl import AIPLResponseHeaders
from ai_pipeline_core._llm_core._response_builder import _build_model_response
from ai_pipeline_core._llm_core.model_response import StreamCompletion, TimingData
from ai_pipeline_core._llm_core.request import AttemptRequest, LLMRequest
from ai_pipeline_core._llm_core.types import CoreMessage, Role
from ai_pipeline_core.exceptions import EmptyResponseError
from tests.support.model_catalog import DEFAULT_TEST_MODEL


def _completion_with_response(response: Any, *, usage: Any | None = None) -> StreamCompletion:
    return StreamCompletion(
        response=response,
        usage=usage or {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        raw_headers={},
        aipl_headers=AIPLResponseHeaders(),
        timing=TimingData(started_at=0.0, first_token_at=0.1, finished_at=0.2),
        final_tps=10.0,
    )


def _request() -> AttemptRequest:
    call = LLMRequest(model=DEFAULT_TEST_MODEL, messages=(CoreMessage(role=Role.USER, content="hi"),))
    return AttemptRequest(call=call, model=call.model, attempt_index=0, call_id="call_1")


def _response(
    *,
    content: str = "Hello",
    finish_reason: str = "stop",
    reasoning_content: str | None = None,
    annotations: list[Any] | None = None,
    provider_specific_fields: dict[str, Any] | None = None,
    thinking_blocks: list[dict[str, Any]] | None = None,
) -> SimpleNamespace:
    message = SimpleNamespace(
        content=content,
        role="assistant",
        reasoning_content=reasoning_content,
        annotations=annotations,
        provider_specific_fields=provider_specific_fields,
        thinking_blocks=thinking_blocks,
    )
    return SimpleNamespace(id="resp-1", choices=(SimpleNamespace(message=message, finish_reason=finish_reason),))


def test_null_choices_raises_empty_response_error() -> None:
    """response.choices=None should raise EmptyResponseError, not TypeError."""
    response = _response()
    response.choices = None

    with pytest.raises(EmptyResponseError, match="no choices"):
        _build_model_response(_completion_with_response(response), _request(), prompt_cache_key=None)


def test_empty_choices_raises_empty_response_error() -> None:
    """response.choices=[] should raise EmptyResponseError."""
    response = _response()
    response.choices = []

    with pytest.raises(EmptyResponseError, match="no choices"):
        _build_model_response(_completion_with_response(response), _request(), prompt_cache_key=None)


def test_empty_response_raises_empty_response_error() -> None:
    """Empty response raises EmptyResponseError with model info in message."""
    with pytest.raises(EmptyResponseError, match=f"Empty response content from model={DEFAULT_TEST_MODEL.name}"):
        _build_model_response(_completion_with_response(_response(content="")), _request(), prompt_cache_key=None)


def test_empty_content_with_reasoning_signal_is_allowed() -> None:
    """Some providers return reasoning metadata without visible content."""
    completion = _completion_with_response(
        _response(content="", provider_specific_fields={"thought_signatures": "sig"}),
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    )

    model_response = _build_model_response(completion, _request(), prompt_cache_key=None)

    assert model_response.content == ""
    assert model_response.provider_specific_fields == {"thought_signatures": "sig"}


def test_missing_annotations_attribute_does_not_crash() -> None:
    """Providers omitting message.annotations must not raise AttributeError."""
    message = SimpleNamespace(content="ok", role="assistant")
    choice = SimpleNamespace(message=message, finish_reason="stop")
    response = SimpleNamespace(id="resp-1", choices=(choice,))

    model_response = _build_model_response(_completion_with_response(response), _request(), prompt_cache_key=None)

    assert model_response.content == "ok"
    assert model_response.citations == ()
