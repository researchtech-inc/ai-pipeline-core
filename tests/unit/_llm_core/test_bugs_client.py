"""Bug-proving tests for LLM response normalization."""

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from ai_pipeline_core._llm_core._aipl import AIPLResponseHeaders
from ai_pipeline_core._llm_core._response_builder import _build_model_response
from ai_pipeline_core._llm_core.model_response import StreamCompletion, TimingData
from ai_pipeline_core._llm_core.request import AttemptRequest, LLMRequest, ResponseSpec
from ai_pipeline_core._llm_core.types import CoreMessage, Role
from ai_pipeline_core.exceptions import EmptyResponseError
from tests.support.model_catalog import DEFAULT_TEST_MODEL


class _Stub(BaseModel):
    """Tiny schema for blank-content structured-output coverage."""

    value: str


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


def test_blank_structured_content_raises_empty_response_error() -> None:
    """Structured request returning blank content alongside reasoning surfaces as empty, not prose-contamination.

    Sets the JSON-repair retry on the right path: empty content has no JSON to repair,
    so callers must retry on a sibling deployment instead of mutating the prompt.
    """
    base = _request()
    request = replace(base, call=replace(base.call, response=ResponseSpec(format=_Stub)))
    completion = _completion_with_response(
        _response(content="   \n  ", provider_specific_fields={"thought_signatures": "sig"}),
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    )

    with pytest.raises(EmptyResponseError, match="empty"):
        _build_model_response(completion, request, prompt_cache_key=None)


def test_empty_code_fence_structured_content_raises_empty_response_error() -> None:
    """A bare ```json\n``` payload raises EmptyResponseError, not ProseContaminationError.

    Regression guard: ``_strip_code_fence`` returns an empty string for a code
    fence with no body, which previously fed ``json.loads("")`` and wrapped as
    ``ProseContaminationError``. Empty fence is semantically the same as blank
    content — retry on a sibling deployment, not via JSON-repair prompt.
    """
    base = _request()
    request = replace(base, call=replace(base.call, response=ResponseSpec(format=_Stub)))
    completion = _completion_with_response(
        _response(content="```json\n```", provider_specific_fields={"thought_signatures": "sig"}),
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    )

    with pytest.raises(EmptyResponseError, match="empty code fence"):
        _build_model_response(completion, request, prompt_cache_key=None)


def test_missing_annotations_attribute_does_not_crash() -> None:
    """Providers omitting message.annotations must not raise AttributeError."""
    message = SimpleNamespace(content="ok", role="assistant")
    choice = SimpleNamespace(message=message, finish_reason="stop")
    response = SimpleNamespace(id="resp-1", choices=(choice,))

    model_response = _build_model_response(_completion_with_response(response), _request(), prompt_cache_key=None)

    assert model_response.content == "ok"
    assert model_response.citations == ()


def _completion_with_headers(response: Any, *, deployment_id: str) -> StreamCompletion:
    raw = {"x-aipl-call-id": "call-empty", "x-aipl-deployment-id": deployment_id, "x-aipl-group-status": "ok"}
    return StreamCompletion(
        response=response,
        usage={"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1},
        raw_headers=raw,
        aipl_headers=AIPLResponseHeaders.from_response_headers(raw),
        timing=TimingData(started_at=0.0, first_token_at=None, finished_at=0.1),
        final_tps=0.0,
    )


def test_empty_response_error_carries_deployment_id_from_completion() -> None:
    """The explicit blank-content path stamps the failed deployment on the exception."""
    completion = _completion_with_headers(_response(content=""), deployment_id="dep-empty-A")

    with pytest.raises(EmptyResponseError) as exc_info:
        _build_model_response(completion, _request(), prompt_cache_key=None)

    assert exc_info.value.deployment_id == "dep-empty-A"
    assert exc_info.value.response_headers["x-aipl-deployment-id"] == "dep-empty-A"


def test_empty_response_error_attached_when_load_json_payload_raises() -> None:
    """``_load_json_payload`` raises bare ``EmptyResponseError``; the wrapper must attach context."""
    base = _request()
    request = replace(base, call=replace(base.call, response=ResponseSpec(format=_Stub)))
    completion = _completion_with_headers(
        _response(content="```json\n```", provider_specific_fields={"thought_signatures": "sig"}),
        deployment_id="dep-fence-A",
    )

    with pytest.raises(EmptyResponseError) as exc_info:
        _build_model_response(completion, request, prompt_cache_key=None)

    assert exc_info.value.deployment_id == "dep-fence-A"
    assert exc_info.value.response_headers["x-aipl-deployment-id"] == "dep-fence-A"
