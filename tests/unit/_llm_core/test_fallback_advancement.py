"""Unit tests for LLM fallback advancement contract."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

from ai_pipeline_core._llm_core import CoreMessage, LLMRequest, RetrySpec, Role
from ai_pipeline_core._llm_core import _transport
from ai_pipeline_core._llm_core import client as llm_client
from ai_pipeline_core.exceptions import LLMError, TerminalError
from ai_pipeline_core.llm import AIModel
from tests.support.model_catalog import DEFAULT_TEST_MODEL, ALTERNATE_TEST_MODEL


class _AsyncChunks:
    """Minimal async iterator over fake stream chunks."""

    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = chunks
        self._index = 0

    def __aiter__(self) -> _AsyncChunks:
        return self

    async def __anext__(self) -> Any:
        if self._index >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk


class _RawResponse:
    """Minimal raw response object consumed by StreamSession."""

    def __init__(self, *, headers: dict[str, str], chunks: list[Any]) -> None:
        self.headers = headers
        self._chunks = chunks

    def parse(self) -> _AsyncChunks:
        """Return fake streamed chunks."""
        return _AsyncChunks(self._chunks)

    async def aclose(self) -> None:
        """Close the fake response."""


def _request(model: AIModel, *, retries: int = 0, retry_delay_seconds: float = 0.0) -> LLMRequest:
    """Build a fallback-test request. Retries default to 0 for clean traces."""
    return LLMRequest(
        model=model,
        messages=(CoreMessage(role=Role.USER, content="hello"),),
        retry=RetrySpec(retries=retries, retry_delay_seconds=retry_delay_seconds),
    )


def _success_raw(model_name: str) -> _RawResponse:
    """Build a successful fake streaming response."""
    delta = SimpleNamespace(role="assistant", content="fallback ok")
    chunk = SimpleNamespace(
        id="resp-fallback",
        usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        choices=[SimpleNamespace(delta=delta, finish_reason="stop")],
    )
    return _RawResponse(
        headers={
            "x-aipl-call-id": "call-fallback",
            "x-aipl-deployment-id": f"deployment-{model_name}",
            "x-aipl-provider": "fake-provider",
            "x-aipl-group-status": "ok",
        },
        chunks=[chunk],
    )


@pytest.mark.asyncio
async def test_group_exhausted_exception_advances_to_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed attempt whose response headers carry ``group_status=exhausted`` advances the AIModel fallback chain."""
    fallback = ALTERNATE_TEST_MODEL
    primary = DEFAULT_TEST_MODEL.model_copy(update={"fallback": fallback})
    seen_models: list[str] = []

    @asynccontextmanager
    async def fake_open_stream(
        req: Any, *, messages: list[dict[str, Any]], api_kwargs: dict[str, Any]
    ) -> AsyncIterator[_RawResponse]:
        _ = messages, api_kwargs
        seen_models.append(req.model.name)
        if req.model.name == primary.name:
            request = httpx.Request("POST", "http://proxy/v1/chat/completions")
            response = httpx.Response(
                503,
                request=request,
                headers={
                    "x-aipl-call-id": "call-primary",
                    "x-aipl-deployment-id": f"deployment-{req.model.name}",
                    "x-aipl-group-status": "exhausted",
                },
            )
            raise openai.InternalServerError("group exhausted", response=response, body=None)
        yield _success_raw(req.model.name)

    monkeypatch.setattr(_transport, "open_stream", fake_open_stream)

    response = await llm_client.generate(_request(primary))

    assert response.content == "fallback ok"
    assert response.model == fallback.name
    assert seen_models == [primary.name, fallback.name]
    assert response.transport.model_chain.requested == primary.name
    assert response.transport.model_chain.active == fallback.name
    assert response.transport.model_chain.used_fallback is True


@pytest.mark.asyncio
async def test_successful_response_with_exhausted_header_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful response whose headers say ``group_status=exhausted`` is returned, not discarded."""
    fallback = ALTERNATE_TEST_MODEL
    primary = DEFAULT_TEST_MODEL.model_copy(update={"fallback": fallback})
    seen_models: list[str] = []

    delta = SimpleNamespace(role="assistant", content="primary ok")
    chunk = SimpleNamespace(
        id="resp-primary",
        usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        choices=[SimpleNamespace(delta=delta, finish_reason="stop")],
    )
    exhausted_success = _RawResponse(
        headers={
            "x-aipl-call-id": "call-primary",
            "x-aipl-deployment-id": "deployment-primary",
            "x-aipl-provider": "fake-provider",
            "x-aipl-group-status": "exhausted",
        },
        chunks=[chunk],
    )

    @asynccontextmanager
    async def fake_open_stream(
        req: Any, *, messages: list[dict[str, Any]], api_kwargs: dict[str, Any]
    ) -> AsyncIterator[_RawResponse]:
        _ = messages, api_kwargs
        seen_models.append(req.model.name)
        yield exhausted_success

    monkeypatch.setattr(_transport, "open_stream", fake_open_stream)

    response = await llm_client.generate(_request(primary))

    assert response.content == "primary ok"
    assert response.model == primary.name
    assert seen_models == [primary.name]  # fallback NOT walked
    assert response.transport.aipl.group_status == "exhausted"


@pytest.mark.asyncio
async def test_fallback_model_retries_after_primary_exhaustion(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exhausted flag is per-model: after advancing, the fallback's first
    retryable failure must not short-circuit because the primary was exhausted."""
    fallback = ALTERNATE_TEST_MODEL
    primary = DEFAULT_TEST_MODEL.model_copy(update={"fallback": fallback})
    seen: list[tuple[str, int]] = []

    @asynccontextmanager
    async def fake_open_stream(
        req: Any, *, messages: list[dict[str, Any]], api_kwargs: dict[str, Any]
    ) -> AsyncIterator[_RawResponse]:
        _ = messages, api_kwargs
        attempt = req.attempt_index
        seen.append((req.model.name, attempt))
        if req.model.name == primary.name:
            # Primary exhausts the group on its first attempt.
            request = httpx.Request("POST", "http://proxy/v1/chat/completions")
            response = httpx.Response(
                503,
                request=request,
                headers={
                    "x-aipl-call-id": "call-primary",
                    "x-aipl-deployment-id": "deployment-primary",
                    "x-aipl-group-status": "exhausted",
                },
            )
            raise openai.InternalServerError("group exhausted", response=response, body=None)
        if attempt == 0:
            # Fallback's first attempt: retryable transient. If the exhausted
            # flag leaks across models, the retry loop would early-return here.
            request = httpx.Request("POST", "http://proxy/v1/chat/completions")
            response = httpx.Response(
                503,
                request=request,
                headers={
                    "x-aipl-call-id": "call-fallback-attempt0",
                    "x-aipl-deployment-id": "deployment-fallback",
                    "x-aipl-group-status": "ok",
                },
            )
            raise openai.InternalServerError("transient", response=response, body=None)
        yield _success_raw(req.model.name)

    monkeypatch.setattr(_transport, "open_stream", fake_open_stream)

    response = await llm_client.generate(_request(primary, retries=2, retry_delay_seconds=0.0))

    assert response.content == "fallback ok"
    assert response.model == fallback.name
    # Primary attempted once, fallback attempted twice (retried after transient failure).
    assert seen == [(primary.name, 0), (fallback.name, 0), (fallback.name, 1)]


@pytest.mark.asyncio
async def test_terminal_bad_request_does_not_advance_to_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Terminal provider errors must not walk the fallback chain."""
    fallback = ALTERNATE_TEST_MODEL
    primary = DEFAULT_TEST_MODEL.model_copy(update={"fallback": fallback})
    seen_models: list[str] = []

    @asynccontextmanager
    async def fake_open_stream(
        req: Any, *, messages: list[dict[str, Any]], api_kwargs: dict[str, Any]
    ) -> AsyncIterator[_RawResponse]:
        _ = messages, api_kwargs
        seen_models.append(req.model.name)
        if req.model.name == primary.name:
            response = httpx.Response(400, request=httpx.Request("POST", "http://proxy/v1/chat/completions"))
            raise openai.BadRequestError("bad request", response=response, body=None)
        yield _success_raw(req.model.name)

    monkeypatch.setattr(_transport, "open_stream", fake_open_stream)

    with pytest.raises(TerminalError):
        await llm_client.generate(_request(primary))

    assert seen_models == [primary.name]


@pytest.mark.asyncio
async def test_auth_failure_without_group_exhaustion_does_not_advance_to_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auth-like failures are terminal for the model chain unless the proxy marks the group exhausted."""
    fallback = ALTERNATE_TEST_MODEL
    primary = DEFAULT_TEST_MODEL.model_copy(update={"fallback": fallback})
    seen_models: list[str] = []

    @asynccontextmanager
    async def fake_open_stream(
        req: Any, *, messages: list[dict[str, Any]], api_kwargs: dict[str, Any]
    ) -> AsyncIterator[_RawResponse]:
        _ = messages, api_kwargs
        seen_models.append(req.model.name)
        if req.model.name == primary.name:
            request = httpx.Request("POST", "http://proxy/v1/chat/completions")
            response = httpx.Response(
                401,
                request=request,
                headers={"x-aipl-deployment-id": f"deployment-{req.model.name}", "x-aipl-group-status": "ok"},
            )
            raise httpx.HTTPStatusError("unauthorized", request=request, response=response)
        yield _success_raw(req.model.name)

    monkeypatch.setattr(_transport, "open_stream", fake_open_stream)

    with pytest.raises(LLMError):
        await llm_client.generate(_request(primary))

    assert seen_models == [primary.name]
