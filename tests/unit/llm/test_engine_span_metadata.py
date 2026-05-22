"""LLM round span metadata tests."""

from uuid import uuid7

from ai_pipeline_core._llm_core.model_response import Citation, ModelResponse
from ai_pipeline_core._llm_core.request import LLMRequest
from ai_pipeline_core._llm_core.types import CoreMessage, Role, TokenUsage
from ai_pipeline_core.llm._engine import _record_round_meta
from ai_pipeline_core.pipeline._span_types import SpanContext
from tests.support.model_catalog import DEFAULT_TEST_MODEL


def _request() -> LLMRequest:
    return LLMRequest(model=DEFAULT_TEST_MODEL, messages=(CoreMessage(role=Role.USER, content="hi"),))


def _response(*, citations: tuple[Citation, ...] = ()) -> ModelResponse[str]:
    return ModelResponse[str](
        content="answer",
        parsed="answer",
        usage=TokenUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3),
        cost=0.01,
        model="test-model",
        citations=citations,
    )


def test_llm_round_meta_includes_serialized_citations() -> None:
    ctx = SpanContext(span_id=uuid7(), parent_span_id=None)
    citation = Citation(title="Source", url="https://example.com", start_index=0, end_index=6)

    _record_round_meta(ctx, _response(citations=(citation,)), _request())

    assert ctx._meta["citations"] == [
        {"title": "Source", "url": "https://example.com", "start_index": 0, "end_index": 6}
    ]


def test_llm_round_meta_includes_empty_citations_list() -> None:
    ctx = SpanContext(span_id=uuid7(), parent_span_id=None)

    _record_round_meta(ctx, _response(), _request())

    assert ctx._meta["citations"] == []
