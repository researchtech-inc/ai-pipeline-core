"""Unit tests for LLM fallback advancement contract."""

import asyncio
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
from ai_pipeline_core._llm_core._watchdog import StreamWatchdog, WatchdogConfig
from ai_pipeline_core._llm_core.exceptions import (
    EmptyResponseError,
    MidStreamProviderError,
    StreamWatchdogError,
)
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


def _content_filter_raw(deployment_id: str) -> _RawResponse:
    """Build a successful 200 stream with finish_reason=content_filter (provider refusal)."""
    delta = SimpleNamespace(role="assistant", content="I cannot help with that.")
    chunk = SimpleNamespace(
        id="resp-refused",
        usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        choices=[SimpleNamespace(delta=delta, finish_reason="content_filter")],
    )
    return _RawResponse(
        headers={
            "x-aipl-call-id": "call-refused",
            "x-aipl-deployment-id": deployment_id,
            "x-aipl-group-status": "ok",
        },
        chunks=[chunk],
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


class _WatchdogChunks:
    """Async iterator that raises StreamWatchdogError on first chunk pull."""

    def __init__(self, *, deployment_id: str) -> None:
        self._deployment_id = deployment_id

    def __aiter__(self) -> _WatchdogChunks:
        return self

    async def __anext__(self) -> Any:
        raise StreamWatchdogError("stall", self._deployment_id, observed_tps=0.5)


class _WatchdogRaw:
    """Raw response whose stream raises StreamWatchdogError mid-drain."""

    def __init__(self, *, headers: dict[str, str], deployment_id: str) -> None:
        self.headers = headers
        self._deployment_id = deployment_id

    def parse(self) -> _WatchdogChunks:
        """Return chunks that raise once iterated."""
        return _WatchdogChunks(deployment_id=self._deployment_id)

    async def aclose(self) -> None:
        """Close the fake response."""


@pytest.mark.asyncio
@pytest.mark.parametrize("proxy_failure_class", ["context_limit", "capability_mismatch", "refusal"])
async def test_proxy_failure_class_advances_to_fallback(
    monkeypatch: pytest.MonkeyPatch, proxy_failure_class: str
) -> None:
    """Proxy-stamped terminal-for-model classes advance the AIModel chain.

    The primary raises a 503 carrying the refined ``x-aipl-failure-class``;
    the framework must walk to ``AIModel.fallback`` instead of exhausting
    retries on the same model. Different models have different context
    limits, capabilities, and content policies — so all three classes always
    try the next entry.
    """
    fallback = ALTERNATE_TEST_MODEL
    primary = DEFAULT_TEST_MODEL.model_copy(update={"fallback": fallback})
    seen_models: list[str] = []

    @asynccontextmanager
    async def fake_open_stream(
        req: Any, *, messages: list[dict[str, Any]], api_kwargs: dict[str, Any]
    ) -> AsyncIterator[Any]:
        _ = messages, api_kwargs
        seen_models.append(req.model.name)
        if req.model.name == primary.name:
            request = httpx.Request("POST", "http://proxy/v1/chat/completions")
            response = httpx.Response(
                503,
                request=request,
                headers={
                    "x-aipl-call-id": "call-primary",
                    "x-aipl-deployment-id": "deployment-primary",
                    "x-aipl-failure-class": proxy_failure_class,
                    "x-aipl-group-status": "ok",
                },
            )
            raise openai.InternalServerError(f"upstream {proxy_failure_class}", response=response, body=None)
        yield _success_raw(req.model.name)

    monkeypatch.setattr(_transport, "open_stream", fake_open_stream)

    response = await llm_client.generate(_request(primary, retries=2, retry_delay_seconds=0.0))

    assert response.content == "fallback ok"
    assert response.model == fallback.name
    assert seen_models == [primary.name, fallback.name]


@pytest.mark.asyncio
@pytest.mark.parametrize("proxy_failure_class", ["context_limit", "capability_mismatch", "refusal"])
async def test_bad_request_with_proxy_failure_class_advances_to_fallback(
    monkeypatch: pytest.MonkeyPatch, proxy_failure_class: str
) -> None:
    """A 400 (BadRequestError) carrying a terminal-for-model class advances the chain.

    Regression guard: ``_execute_attempt`` previously caught ``BadRequestError``
    via ``_TERMINAL_FAILURES`` and raised ``TerminalError`` outright, bypassing
    proxy ``x-aipl-failure-class`` consumption. The fix routes terminal failures
    through the proxy-header check first.
    """
    fallback = ALTERNATE_TEST_MODEL
    primary = DEFAULT_TEST_MODEL.model_copy(update={"fallback": fallback})
    seen_models: list[str] = []

    @asynccontextmanager
    async def fake_open_stream(
        req: Any, *, messages: list[dict[str, Any]], api_kwargs: dict[str, Any]
    ) -> AsyncIterator[Any]:
        _ = messages, api_kwargs
        seen_models.append(req.model.name)
        if req.model.name == primary.name:
            request = httpx.Request("POST", "http://proxy/v1/chat/completions")
            response = httpx.Response(
                400,
                request=request,
                headers={
                    "x-aipl-call-id": "call-primary-400",
                    "x-aipl-deployment-id": "deployment-primary",
                    "x-aipl-failure-class": proxy_failure_class,
                    "x-aipl-group-status": "ok",
                },
            )
            raise openai.BadRequestError(f"bad request: {proxy_failure_class}", response=response, body=None)
        yield _success_raw(req.model.name)

    monkeypatch.setattr(_transport, "open_stream", fake_open_stream)

    response = await llm_client.generate(_request(primary, retries=0, retry_delay_seconds=0.0))

    assert response.content == "fallback ok"
    assert response.model == fallback.name
    assert seen_models == [primary.name, fallback.name]


@pytest.mark.asyncio
async def test_bad_request_without_terminal_failure_class_still_raises_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 400 with no recognized failure class still raises ``TerminalError`` (no fallback)."""
    fallback = ALTERNATE_TEST_MODEL
    primary = DEFAULT_TEST_MODEL.model_copy(update={"fallback": fallback})
    seen_models: list[str] = []

    @asynccontextmanager
    async def fake_open_stream(
        req: Any, *, messages: list[dict[str, Any]], api_kwargs: dict[str, Any]
    ) -> AsyncIterator[Any]:
        _ = messages, api_kwargs
        seen_models.append(req.model.name)
        if req.model.name == primary.name:
            request = httpx.Request("POST", "http://proxy/v1/chat/completions")
            response = httpx.Response(400, request=request)
            raise openai.BadRequestError("malformed request", response=response, body=None)
        yield _success_raw(req.model.name)

    monkeypatch.setattr(_transport, "open_stream", fake_open_stream)

    with pytest.raises(TerminalError):
        await llm_client.generate(_request(primary))

    assert seen_models == [primary.name]


@pytest.mark.asyncio
async def test_content_filter_advances_to_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 response with ``finish_reason=content_filter`` advances the AIModel chain.

    Regression guard: ``ContentPolicyError`` previously classified as ``"policy"``
    which set ``skip_deployment=True`` and burned retries on sibling deployments
    of the same model — even though sibling deployments of the same model
    typically share the same content policy. The fix maps ``ContentPolicyError``
    to ``"refusal"`` so refusals always advance to a different model.
    """
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
            yield _content_filter_raw(deployment_id=f"deployment-{req.model.name}")
            return
        yield _success_raw(req.model.name)

    monkeypatch.setattr(_transport, "open_stream", fake_open_stream)

    response = await llm_client.generate(_request(primary, retries=2, retry_delay_seconds=0.0))

    assert response.content == "fallback ok"
    assert response.model == fallback.name
    assert seen_models == [primary.name, fallback.name]


@pytest.mark.asyncio
async def test_stream_watchdog_kill_appends_deployment_to_next_attempt_skip_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Watchdog kill on attempt 0 must surface the failed deployment in attempt 1's skip list.

    Regression guard for the Gemma/Parasail 1358s retry storm: the previous
    routing returned skip_deployment=False for the watchdog (classified as
    "timeout"), so retries re-pinned the aborted lane via HRW cache affinity.
    """
    primary = DEFAULT_TEST_MODEL
    aborted_deployment = "deployment-aborted-A"
    seen_skip_ids: list[frozenset[str]] = []

    @asynccontextmanager
    async def fake_open_stream(
        req: Any, *, messages: list[dict[str, Any]], api_kwargs: dict[str, Any]
    ) -> AsyncIterator[Any]:
        _ = messages, api_kwargs
        seen_skip_ids.append(frozenset(req.call.routing.skip_ids))
        attempt = req.attempt_index
        if attempt == 0:
            yield _WatchdogRaw(
                headers={
                    "x-aipl-call-id": "call-aborted",
                    "x-aipl-deployment-id": aborted_deployment,
                    "x-aipl-provider": "openrouter-parasail",
                    "x-aipl-group-status": "ok",
                },
                deployment_id=aborted_deployment,
            )
            return
        yield _success_raw(req.model.name)

    monkeypatch.setattr(_transport, "open_stream", fake_open_stream)

    response = await llm_client.generate(_request(primary, retries=1, retry_delay_seconds=0.0))

    assert response.content == "fallback ok"
    assert seen_skip_ids[0] == frozenset()
    assert aborted_deployment in seen_skip_ids[1]


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


def _empty_raw(deployment_id: str) -> _RawResponse:
    """Successful 200 stream with no visible content, tool calls, or reasoning."""
    delta = SimpleNamespace(role="assistant", content="")
    chunk = SimpleNamespace(
        id="resp-empty",
        usage={"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1},
        choices=[SimpleNamespace(delta=delta, finish_reason="stop")],
    )
    return _RawResponse(
        headers={
            "x-aipl-call-id": "call-empty",
            "x-aipl-deployment-id": deployment_id,
            "x-aipl-group-status": "ok",
        },
        chunks=[chunk],
    )


@pytest.mark.asyncio
async def test_empty_response_appends_deployment_to_next_attempt_skip_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``EmptyResponseError`` must surface the failed deployment to the next attempt's skip list."""
    primary = DEFAULT_TEST_MODEL
    empty_deployment = "deployment-empty-A"
    seen_skip_ids: list[frozenset[str]] = []

    @asynccontextmanager
    async def fake_open_stream(
        req: Any, *, messages: list[dict[str, Any]], api_kwargs: dict[str, Any]
    ) -> AsyncIterator[Any]:
        _ = messages, api_kwargs
        seen_skip_ids.append(frozenset(req.call.routing.skip_ids))
        if req.attempt_index == 0:
            yield _empty_raw(deployment_id=empty_deployment)
            return
        yield _success_raw(req.model.name)

    monkeypatch.setattr(_transport, "open_stream", fake_open_stream)

    response = await llm_client.generate(_request(primary, retries=1, retry_delay_seconds=0.0))

    assert response.content == "fallback ok"
    assert seen_skip_ids[0] == frozenset()
    assert empty_deployment in seen_skip_ids[1]


class _MidStreamChunks:
    """Async iterator that yields one content chunk, then raises a provider error."""

    def __init__(self) -> None:
        self._sent = False

    def __aiter__(self) -> _MidStreamChunks:
        return self

    async def __anext__(self) -> Any:
        if self._sent:
            raise OSError("provider stream dropped after partial output")
        self._sent = True
        return SimpleNamespace(
            id="resp-mid",
            usage=None,
            choices=[SimpleNamespace(delta=SimpleNamespace(role="assistant", content="hello"), finish_reason=None)],
        )


class _MidStreamRaw:
    """Raw response whose stream drops mid-flight after committing content."""

    def __init__(self, *, deployment_id: str) -> None:
        self.headers = {
            "x-aipl-call-id": "call-midstream",
            "x-aipl-deployment-id": deployment_id,
            "x-aipl-group-status": "ok",
        }

    def parse(self) -> _MidStreamChunks:
        return _MidStreamChunks()

    async def aclose(self) -> None:
        """Close the fake response."""


@pytest.mark.asyncio
async def test_midstream_failure_appends_deployment_to_next_attempt_skip_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``MidStreamProviderError`` must surface the failed deployment to the next attempt's skip list."""
    primary = DEFAULT_TEST_MODEL
    bad_deployment = "deployment-midstream-A"
    seen_skip_ids: list[frozenset[str]] = []

    @asynccontextmanager
    async def fake_open_stream(
        req: Any, *, messages: list[dict[str, Any]], api_kwargs: dict[str, Any]
    ) -> AsyncIterator[Any]:
        _ = messages, api_kwargs
        seen_skip_ids.append(frozenset(req.call.routing.skip_ids))
        if req.attempt_index == 0:
            yield _MidStreamRaw(deployment_id=bad_deployment)
            return
        yield _success_raw(req.model.name)

    monkeypatch.setattr(_transport, "open_stream", fake_open_stream)

    response = await llm_client.generate(_request(primary, retries=1, retry_delay_seconds=0.0))

    assert response.content == "fallback ok"
    assert seen_skip_ids[0] == frozenset()
    assert bad_deployment in seen_skip_ids[1]


class _BlockingChunks:
    """Async iterator that never produces a chunk, letting the watchdog TTFT gate fire."""

    def __init__(self, closed: asyncio.Event) -> None:
        self._closed = closed

    def __aiter__(self) -> _BlockingChunks:
        return self

    async def __anext__(self) -> Any:
        await self._closed.wait()
        raise StopAsyncIteration


class _BlockingRaw:
    def __init__(self, *, deployment_id: str) -> None:
        self.closed = asyncio.Event()
        self.headers = {
            "x-aipl-call-id": "call-blocking",
            "x-aipl-deployment-id": deployment_id,
            "x-aipl-group-status": "ok",
        }

    def parse(self) -> _BlockingChunks:
        return _BlockingChunks(self.closed)

    async def aclose(self) -> None:
        self.closed.set()


@pytest.mark.asyncio
async def test_real_watchdog_uses_response_header_deployment_for_skip_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end proof that the live watchdog reads ``deployment_id`` from response headers.

    The first attempt opens a stream that never emits a first chunk; the
    TTFT gate fires inside ``StreamSession`` and raises. The framework must
    attribute the kill to the deployment carried on the response header
    (not the unset ``force_deployment_id``) so the retry skips it.
    """
    primary = DEFAULT_TEST_MODEL
    deployment_id = "deployment-from-response-header"
    seen_skip_ids: list[frozenset[str]] = []

    def fast_watchdog(_req: Any) -> StreamWatchdog:
        return StreamWatchdog(
            config=WatchdogConfig(
                ttft_seconds=0.05,
                inactivity_seconds=0.05,
                total_wall_seconds=0.5,
                tick_seconds=0.01,
            ),
        )

    @asynccontextmanager
    async def fake_open_stream(
        req: Any, *, messages: list[dict[str, Any]], api_kwargs: dict[str, Any]
    ) -> AsyncIterator[Any]:
        _ = messages, api_kwargs
        seen_skip_ids.append(frozenset(req.call.routing.skip_ids))
        if req.attempt_index == 0:
            yield _BlockingRaw(deployment_id=deployment_id)
            return
        yield _success_raw(req.model.name)

    monkeypatch.setattr(llm_client, "_make_watchdog", fast_watchdog)
    monkeypatch.setattr(_transport, "open_stream", fake_open_stream)

    response = await llm_client.generate(_request(primary, retries=1, retry_delay_seconds=0.0))

    assert response.content == "fallback ok"
    assert seen_skip_ids[0] == frozenset()
    assert deployment_id in seen_skip_ids[1]


def test_midstream_provider_error_constructible_with_deployment_context() -> None:
    """Sanity check the wiring we exercise above: the exception carries the attribution."""
    exc = MidStreamProviderError(
        "drop",
        deployment_id="dep-A",
        response_headers={"x-aipl-deployment-id": "dep-A"},
    )
    assert exc.deployment_id == "dep-A"
    assert exc.response_headers["x-aipl-deployment-id"] == "dep-A"


def test_empty_response_error_constructible_with_deployment_context() -> None:
    exc = EmptyResponseError(
        "blank",
        deployment_id="dep-B",
        response_headers={"x-aipl-deployment-id": "dep-B"},
    )
    assert exc.deployment_id == "dep-B"
    assert exc.response_headers["x-aipl-deployment-id"] == "dep-B"
