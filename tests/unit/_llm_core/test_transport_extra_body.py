"""Transport extra_body construction tests."""

from ai_pipeline_core._llm_core._transport import build_extra_body
from ai_pipeline_core._llm_core.request import AttemptRequest, CacheSpec, LLMRequest
from ai_pipeline_core._llm_core.types import CoreMessage, Role
from tests.support.model_catalog import DEFAULT_TEST_MODEL


def _attempt(*, attempt_index: int = 0, cache: CacheSpec | None = None) -> AttemptRequest:
    request = LLMRequest(
        model=DEFAULT_TEST_MODEL,
        messages=(CoreMessage(role=Role.USER, content="hi"),),
        cache=cache or CacheSpec(ttl=0),
    )
    return AttemptRequest(call=request, model=request.model, attempt_index=attempt_index, call_id="call_1")


def test_build_extra_body_baseline_is_empty() -> None:
    assert build_extra_body(_attempt(), prompt_cache_key=None) == {}


def test_build_extra_body_includes_prompt_cache_key() -> None:
    assert build_extra_body(_attempt(), prompt_cache_key="cache-key") == {"prompt_cache_key": "cache-key"}


def test_build_extra_body_disables_cache_on_retry() -> None:
    body = build_extra_body(_attempt(attempt_index=1), prompt_cache_key=None)

    assert body == {"cache": {"no-cache": True, "no-store": True}}


def test_build_extra_body_disables_cache_when_bypassing_response_cache() -> None:
    body = build_extra_body(_attempt(cache=CacheSpec(ttl=0, bypass_response_cache=True)), prompt_cache_key=None)

    assert body == {"cache": {"no-cache": True, "no-store": True}}


def test_build_extra_body_never_injects_usage_key() -> None:
    body = build_extra_body(_attempt(attempt_index=1), prompt_cache_key="cache-key")

    assert "usage" not in body
