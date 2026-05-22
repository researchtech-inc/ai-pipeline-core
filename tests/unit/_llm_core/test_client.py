"""Unit tests for the streaming LLM transport and response helpers."""

import base64
import struct
import zlib
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from ai_pipeline_core._llm_core._aipl import AIPLResponseHeaders
from ai_pipeline_core._llm_core.client import (
    _STRUCTURED_BASE_SYSTEM_PROMPT,
    _STRUCTURED_RETRY_SYSTEM_PROMPT,
    _prepare_api_kwargs,
    _with_structured_retry_prompt,
)
from ai_pipeline_core._llm_core._response_builder import _build_model_response
from ai_pipeline_core._llm_core._transport import (
    _content_to_api_parts,
    annotate_with_cache_markers,
    build_extra_body,
    compute_cache_key,
    core_messages_to_api,
)
from ai_pipeline_core._llm_core.model_response import StreamCompletion, TimingData
from ai_pipeline_core._llm_core.request import AttemptRequest, CacheSpec, LLMRequest, ResponseSpec
from ai_pipeline_core._llm_core.types import CoreMessage, ImageContent, PDFContent, RawToolCall, Role, TextContent
from ai_pipeline_core.exceptions import ContentPolicyError, EmptyResponseError
from tests.support.model_catalog import DEFAULT_TEST_MODEL


def _make_png() -> bytes:
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    ihdr_crc = zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF
    ihdr = struct.pack(">I", 13) + b"IHDR" + ihdr_data + struct.pack(">I", ihdr_crc)
    raw = zlib.compress(b"\x00\x00\x00\x00")
    idat_crc = zlib.crc32(b"IDAT" + raw) & 0xFFFFFFFF
    idat = struct.pack(">I", len(raw)) + b"IDAT" + raw + struct.pack(">I", idat_crc)
    iend_crc = zlib.crc32(b"IEND") & 0xFFFFFFFF
    iend = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", iend_crc)
    return sig + ihdr + idat + iend


def _make_completion(
    *,
    content: str = "Hello",
    finish_reason: str = "stop",
    prompt: int = 10,
    completion_tokens: int = 5,
    reasoning_content: str | None = None,
    annotations: list[Any] | None = None,
    tool_calls: list[Any] | None = None,
    provider_specific_fields: dict[str, Any] | None = None,
    thinking_blocks: list[dict[str, Any]] | None = None,
    response_cost: float | None = None,
    refusal: str | None = None,
) -> StreamCompletion:
    msg = SimpleNamespace(
        content=content,
        role="assistant",
        refusal=refusal,
        reasoning_content=reasoning_content,
        annotations=annotations or [],
        tool_calls=tool_calls or [],
        provider_specific_fields=provider_specific_fields,
        thinking_blocks=thinking_blocks,
    )
    response = SimpleNamespace(
        id="resp-1",
        choices=(SimpleNamespace(message=msg, finish_reason=finish_reason),),
    )
    return StreamCompletion(
        response=response,
        usage={"prompt_tokens": prompt, "completion_tokens": completion_tokens, "total_tokens": prompt + completion_tokens},
        raw_headers={},
        aipl_headers=AIPLResponseHeaders(response_cost=response_cost),
        timing=TimingData(started_at=0.0, first_token_at=0.25, finished_at=1.0),
        final_tps=10.0,
    )


def _make_attempt(*, response_format: type[BaseModel] | None = None, attempt_index: int = 0, cache: CacheSpec | None = None) -> AttemptRequest:
    request = LLMRequest(
        model=DEFAULT_TEST_MODEL,
        messages=(CoreMessage(role=Role.USER, content="hi"),),
        response=ResponseSpec(format=response_format),
        cache=cache or CacheSpec(ttl=0),
    )
    return AttemptRequest(call=request, model=request.model, attempt_index=attempt_index, call_id="call_1")


class TestContentToApiParts:
    def test_string_input(self) -> None:
        assert _content_to_api_parts("hello") == [{"type": "text", "text": "hello"}]

    def test_text_content(self) -> None:
        assert _content_to_api_parts(TextContent(text="world")) == [{"type": "text", "text": "world"}]

    def test_image_valid(self) -> None:
        png_data = _make_png()
        result = _content_to_api_parts(ImageContent(data=base64.b64encode(png_data), mime_type="image/png"))

        assert result == [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64.b64encode(png_data).decode()}", "detail": "high"}}]

    def test_image_invalid(self) -> None:
        result = _content_to_api_parts(ImageContent(data=base64.b64encode(b"not-an-image"), mime_type="image/png"))
        assert result == []

    def test_pdf_valid(self) -> None:
        # Real minimal PDF with xref/trailer to pass pypdf validation.
        pdf_data = (
            b"%PDF-1.4\n"
            b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 100 100]>>endobj\n"
            b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n0000000052 00000 n \n0000000093 00000 n \n"
            b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n148\n%%EOF\n"
        )
        result = _content_to_api_parts(PDFContent(data=base64.b64encode(pdf_data), filename="r.pdf"))

        assert result == [
            {
                "type": "file",
                "file": {
                    "file_data": f"data:application/pdf;base64,{base64.b64encode(pdf_data).decode()}",
                    "filename": "r.pdf",
                    "format": "application/pdf",
                },
            }
        ]

    def test_pdf_invalid_dropped(self) -> None:
        # Plain text bytes no longer fall back to text — they get dropped as invalid PDF.
        result = _content_to_api_parts(PDFContent(data=base64.b64encode(b"This is plain text content")))
        assert result == []

    def test_tuple_of_parts(self) -> None:
        result = _content_to_api_parts((TextContent(text="a"), TextContent(text="b")))
        assert result == [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]


class TestMessagesToApi:
    def test_converts_messages(self) -> None:
        result = core_messages_to_api(
            [CoreMessage(role=Role.USER, content="hi")],
            max_inline_file_total_bytes=50_000_000,
        )

        # Single text content collapses to bare string (signature preservation).
        assert result == [{"role": "user", "content": "hi"}]

    def test_skips_empty_multimodal_content(self) -> None:
        image = ImageContent(data=base64.b64encode(b"bad-image"), mime_type="image/png")
        assert (
            core_messages_to_api(
                [CoreMessage(role=Role.USER, content=image)],
                max_inline_file_total_bytes=50_000_000,
            )
            == []
        )

    def test_tool_result_message(self) -> None:
        result = core_messages_to_api(
            [CoreMessage(role=Role.TOOL, content="done", tool_call_id="call_1", name="search")],
            max_inline_file_total_bytes=50_000_000,
        )

        assert result == [{"role": "tool", "tool_call_id": "call_1", "content": "done"}]

    def test_assistant_tool_call_message(self) -> None:
        call = RawToolCall(id="call_1", function_name="search", arguments='{"q": "x"}')
        result = core_messages_to_api(
            [CoreMessage(role=Role.ASSISTANT, content="", tool_calls=(call,))],
            max_inline_file_total_bytes=50_000_000,
        )

        assert result[0]["role"] == "assistant"
        assert result[0]["content"] is None
        assert result[0]["tool_calls"][0]["function"] == {"name": "search", "arguments": '{"q": "x"}'}


class TestCacheHelpers:
    def test_annotate_with_cache_markers(self) -> None:
        messages = [
            {"role": "system", "content": [{"type": "text", "text": "sys"}]},
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        ]

        # 5 minutes -> wire "300s"
        annotate_with_cache_markers(messages, 1, 5)

        assert messages[0]["cache_control"] == {"type": "ephemeral", "ttl": "300s"}
        assert messages[0]["content"][0]["cache_control"] == {"type": "ephemeral", "ttl": "300s"}
        assert "cache_control" not in messages[1]

    def test_annotate_with_cache_markers_disabled(self) -> None:
        messages = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]

        # ttl_minutes=0 disables markers entirely
        annotate_with_cache_markers(messages, 1, 0)

        assert "cache_control" not in messages[0]

    def test_compute_cache_key_is_stable_and_content_sensitive(self) -> None:
        messages = [{"role": "user", "content": [{"type": "text", "text": "test"}]}]

        assert compute_cache_key(messages) == compute_cache_key(messages)
        assert compute_cache_key(messages) != compute_cache_key([{"role": "user", "content": [{"type": "text", "text": "changed"}]}])
        assert len(compute_cache_key(messages)) == 64

    def test_build_extra_body_disables_cache_on_retry(self) -> None:
        attempt = _make_attempt(attempt_index=1, cache=CacheSpec(ttl=0))

        body = build_extra_body(attempt, prompt_cache_key="cache-key")

        assert body["prompt_cache_key"] == "cache-key"
        assert body["cache"] == {"no-cache": True, "no-store": True}
        assert "usage" not in body


class TestStructuredOutputPrompts:
    def test_retry_attempt_keeps_retry_prompt_on_wire(self) -> None:
        class MyModel(BaseModel):
            value: int

        first_attempt = _make_attempt(response_format=MyModel)
        retry_attempt = _with_structured_retry_prompt(
            AttemptRequest(
                call=first_attempt.call,
                model=first_attempt.model,
                attempt_index=1,
                call_id="call_2",
            )
        )

        api_kwargs, _prompt_cache_key = _prepare_api_kwargs(retry_attempt)
        system_message = api_kwargs["messages"][0]

        assert system_message["role"] == "system"
        assert isinstance(system_message["content"], str)
        assert _STRUCTURED_RETRY_SYSTEM_PROMPT in system_message["content"]
        assert _STRUCTURED_BASE_SYSTEM_PROMPT not in system_message["content"]

    def test_retry_attempt_preserves_cache_prefix_on_context(self) -> None:
        """The retry SYSTEM prepend bumps context_count so the cache marker
        still lands on the last cacheable context message, not one short."""

        class MyModel(BaseModel):
            value: int

        messages = (
            CoreMessage(role=Role.USER, content="doc1 — cacheable"),
            CoreMessage(role=Role.USER, content="doc2 — cacheable"),
            CoreMessage(role=Role.USER, content="live question"),
        )
        request = LLMRequest(
            model=DEFAULT_TEST_MODEL,
            messages=messages,
            response=ResponseSpec(format=MyModel),
            cache=CacheSpec(context_count=2, ttl=5),
        )
        first_attempt = AttemptRequest(call=request, model=request.model, attempt_index=0, call_id="call_1")
        retry_attempt = _with_structured_retry_prompt(
            AttemptRequest(
                call=first_attempt.call,
                model=first_attempt.model,
                attempt_index=1,
                call_id="call_2",
            )
        )

        # The retry prepended a SYSTEM message; context_count must have grown by 1
        # so the cache prefix still covers [SYSTEM(retry), doc1, doc2].
        assert retry_attempt.call.cache.context_count == 3
        assert retry_attempt.call.messages[0].role == Role.SYSTEM
        assert _STRUCTURED_RETRY_SYSTEM_PROMPT in retry_attempt.call.messages[0].content  # type: ignore[operator]  # runtime type wider than declared

        api_kwargs, _cache_key = _prepare_api_kwargs(retry_attempt)
        api_messages = api_kwargs["messages"]
        # Cache boundary marker lands on api_messages[2] (the second cacheable doc).
        assert api_messages[2]["role"] == "user"
        assert api_messages[2].get("cache_control") == {"type": "ephemeral", "ttl": "300s"}
        # The live "question" message MUST be outside the cache prefix.
        assert "cache_control" not in api_messages[3]


class TestBuildModelResponse:
    def test_basic(self) -> None:
        response = _build_model_response(_make_completion(content="Answer"), _make_attempt(), prompt_cache_key=None)

        assert response.content == "Answer"
        assert response.parsed == "Answer"
        assert response.usage.prompt_tokens == 10
        assert response.model == DEFAULT_TEST_MODEL.name

    def test_empty_content_raises(self) -> None:
        with pytest.raises(EmptyResponseError, match="Empty response content"):
            _build_model_response(_make_completion(content=""), _make_attempt(), prompt_cache_key=None)

    def test_content_filter_raises_policy_error(self) -> None:
        with pytest.raises(ContentPolicyError):
            _build_model_response(_make_completion(content="", finish_reason="content_filter"), _make_attempt(), prompt_cache_key=None)

    def test_refusal_field_no_longer_raises_policy_error(self) -> None:
        """The refusal field is no longer extracted; only ``finish_reason == 'content_filter'`` raises ContentPolicyError.

        Round05 verified LiteLLM has no ``refusal`` field on its Message/Delta models;
        refusal-style finish reasons normalize to ``content_filter``.
        """
        response = _build_model_response(
            _make_completion(content="I cannot help with that request.", refusal="I cannot comply with that request."),
            _make_attempt(),
            prompt_cache_key=None,
        )
        assert response.content == "I cannot help with that request."

    def test_structured_output(self) -> None:
        class MyModel(BaseModel):
            value: int

        response = _build_model_response(_make_completion(content='{"value": 42}'), _make_attempt(response_format=MyModel), prompt_cache_key=None)

        assert isinstance(response.parsed, MyModel)
        assert response.parsed.value == 42

    def test_strict_mode_extras_inside_optional_object_are_caught(self) -> None:
        """Extras on a non-None branch of ``Optional[Child]`` must raise.

        Regression: when the schema allows null OR child-object, the
        ``{"type": "null"}`` branch used to count as "no errors" for any
        non-None value, silently masking extras on the child branch.
        """
        from ai_pipeline_core.exceptions import StrictModeViolationError

        class Child(BaseModel):
            a: int

        class Parent(BaseModel):
            child: Child | None = None

        payload = '{"child": {"a": 1, "extra": 2}}'
        with pytest.raises(StrictModeViolationError, match="extra"):
            _build_model_response(_make_completion(content=payload), _make_attempt(response_format=Parent), prompt_cache_key=None)

    def test_strict_mode_null_value_on_optional_passes(self) -> None:
        """``Optional[Child]`` with ``null`` matches the null branch cleanly."""

        class Child(BaseModel):
            a: int

        class Parent(BaseModel):
            child: Child | None = None

        response = _build_model_response(
            _make_completion(content='{"child": null}'),
            _make_attempt(response_format=Parent),
            prompt_cache_key=None,
        )
        assert isinstance(response.parsed, Parent)
        assert response.parsed.child is None

    def test_citations(self) -> None:
        citation = SimpleNamespace(
            type="url_citation",
            url_citation=SimpleNamespace(title="Source", url="https://example.com", start_index=0, end_index=5),
        )

        response = _build_model_response(_make_completion(content="Answer", annotations=[citation]), _make_attempt(), prompt_cache_key=None)

        assert len(response.citations) == 1
        assert response.citations[0].title == "Source"

    def test_cost_from_header(self) -> None:
        response = _build_model_response(_make_completion(content="ok", response_cost=0.05), _make_attempt(), prompt_cache_key=None)

        assert response.cost == 0.05
        assert response.transport.litellm.response_cost == 0.05

    def test_tool_calls_are_preserved(self) -> None:
        raw_call = SimpleNamespace(id="call_1", function=SimpleNamespace(name="lookup", arguments='{"id": 1}'))
        completion = _make_completion(content="", finish_reason="tool_calls", tool_calls=[raw_call])

        response = _build_model_response(completion, _make_attempt(), prompt_cache_key=None)

        assert response.tool_calls == (RawToolCall(id="call_1", function_name="lookup", arguments='{"id": 1}'),)

    def test_provider_specific_fields_and_thinking_blocks(self) -> None:
        response = _build_model_response(
            _make_completion(
                content="answer",
                provider_specific_fields={"thought_signatures": "abc"},
                thinking_blocks=[{"type": "thinking", "thinking": "hmm"}],
            ),
            _make_attempt(),
            prompt_cache_key=None,
        )

        assert response.provider_specific_fields == {"thought_signatures": "abc"}
        assert response.thinking_blocks == ({"type": "thinking", "thinking": "hmm"},)

    def test_chain_of_thought_provider_field_counts_as_reasoning_signal(self) -> None:
        response = _build_model_response(
            _make_completion(content="", provider_specific_fields={"chain_of_thought": "hidden reasoning"}),
            _make_attempt(),
            prompt_cache_key=None,
        )

        assert response.provider_specific_fields == {"chain_of_thought": "hidden reasoning"}
