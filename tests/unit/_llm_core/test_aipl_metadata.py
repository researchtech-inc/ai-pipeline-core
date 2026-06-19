"""AIPL metadata and trace helper tests."""

import logging
from dataclasses import replace
from typing import Any

import httpx
import pytest

from ai_pipeline_core._llm_core import _aipl
from ai_pipeline_core._llm_core import _transport
from ai_pipeline_core._llm_core._config import LLMCoreConfig, override_config
from ai_pipeline_core._llm_core._routing import _CallContext
from ai_pipeline_core._llm_core._transport_metadata import AIPLInfo
from ai_pipeline_core._llm_core.client import _prepare_api_kwargs
from ai_pipeline_core._llm_core.exceptions import (
    ContentPolicyError,
    EmptyResponseError,
    MidStreamProviderError,
    PartialToolCallStreamError,
    StreamWatchdogError,
)
from ai_pipeline_core._llm_core.model_response import AttemptOutcome
from ai_pipeline_core._llm_core.request import AttemptRequest, LLMRequest
from ai_pipeline_core._llm_core.request import AttemptCorrelation, CacheSpec, RetrySpec, TenantSpec
from ai_pipeline_core._llm_core.types import CoreMessage, Role
from tests.support.model_catalog import ALTERNATE_TEST_MODEL, DEFAULT_TEST_MODEL


def _attempt() -> AttemptRequest:
    request = LLMRequest(
        model=DEFAULT_TEST_MODEL,
        messages=(CoreMessage(role=Role.USER, content="hi"),),
    )
    return AttemptRequest(call=request, model=request.model, attempt_index=0, call_id="call-123")


def _attribution_request(*, tenant: TenantSpec | None = None, cache: CacheSpec | None = None) -> LLMRequest:
    return LLMRequest(
        model=DEFAULT_TEST_MODEL,
        messages=(
            CoreMessage(role=Role.USER, content="cacheable context"),
            CoreMessage(role=Role.USER, content="question"),
        ),
        cache=cache or CacheSpec(ttl=0),
        retry=RetrySpec(retries=0),
        tenant=tenant or TenantSpec(),
        purpose="unit-attribution",
    )


def _attribution_attempt(request: LLMRequest, *, attempt_index: int = 0) -> AttemptRequest:
    return AttemptRequest(
        call=request,
        model=request.model,
        attempt_index=attempt_index,
        call_id="call-1",
        correlation=AttemptCorrelation(
            logical_request_id="logical-1",
            attempt_number=2,
            attempt_kind="fallback_model",
            is_fallback=True,
            requested_model=request.model.name,
            prev_call_id="call-0",
            prev_model="previous-model",
            prev_deployment_id="deployment-0",
            prev_failure_class="rate",
        ),
    )


def test_build_aipl_metadata_includes_aipl_call_id() -> None:
    metadata = _aipl.build_aipl_metadata(_attempt())

    assert metadata == {"aipl": "1", "aipl_call_id": "call-123"}


def test_proxy_headers_include_correlation_and_sanitized_tenant() -> None:
    request = _attribution_request(
        tenant=TenantSpec(
            project="tenant\nproject",
            workload_stage="stage\rname",
            run_id="run\x00id",
            workload_kind="summary",
        )
    )

    with override_config(LLMCoreConfig(openai_base_url="https://proxy.example/v1", openai_api_key="key")):
        headers = _transport.build_request_headers(_attribution_attempt(request))

    assert headers == {
        "x-aipl-call-id": "call-1",
        "x-aipl-logical-request-id": "logical-1",
        "x-aipl-attempt-number": "2",
        "x-aipl-attempt-kind": "fallback_model",
        "x-aipl-requested-model": DEFAULT_TEST_MODEL.name,
        "x-aipl-is-fallback": "true",
        "x-aipl-prev-call-id": "call-0",
        "x-aipl-prev-model": "previous-model",
        "x-aipl-prev-deployment-id": "deployment-0",
        "x-aipl-prev-failure-class": "rate",
        "x-aipl-project": "tenantproject",
        "x-aipl-workload-stage": "stagename",
        "x-aipl-run-id": "runid",
        "x-aipl-workload-kind": "summary",
    }


def test_proxy_headers_are_ascii_safe_for_httpx() -> None:
    request = _attribution_request(
        tenant=TenantSpec(
            project="café-project",
            workload_stage="résumé\x85stage",
            run_id="run-id",
            workload_kind="summary",
        )
    )
    attempt = _attribution_attempt(request)
    correlation = replace(
        attempt.correlation,
        requested_model="模型-model",
        prev_model="modèle-prev",
        prev_deployment_id="déploiement-1\x85",
    )
    attempt = replace(attempt, correlation=correlation)

    with override_config(LLMCoreConfig(openai_base_url="https://proxy.example/v1", openai_api_key="key")):
        headers = _transport.build_request_headers(attempt)

    httpx.Headers(headers)
    assert all(value.isascii() for value in headers.values())
    assert headers["x-aipl-requested-model"] == "-model"
    assert headers["x-aipl-prev-model"] == "modle-prev"
    assert headers["x-aipl-prev-deployment-id"] == "dploiement-1"
    assert headers["x-aipl-project"] == "caf-project"
    assert headers["x-aipl-workload-stage"] == "rsumstage"


def test_direct_mode_omits_aipl_headers_metadata_and_cache_protocol() -> None:
    request = _attribution_request(
        tenant=TenantSpec(project="tenant", workload_stage="stage", run_id="run"),
        cache=CacheSpec(context_count=1, ttl=5),
    )
    attempt = _attribution_attempt(request, attempt_index=1)

    with override_config(LLMCoreConfig(openai_base_url="https://openrouter.ai/api/v1", openai_api_key="key")):
        headers = _transport.build_request_headers(attempt)
        api_kwargs, prompt_cache_key = _prepare_api_kwargs(attempt)

    assert headers == {}
    assert prompt_cache_key is None
    assert api_kwargs["user"] == "tenant/stage/run"
    assert "metadata" not in api_kwargs
    assert "extra_body" not in api_kwargs
    assert "cache_control" not in api_kwargs["messages"][0]
    assert "cache_control" not in api_kwargs["messages"][0]["content"]


def test_tenant_user_omits_empty_suffixes_but_preserves_run_position() -> None:
    with override_config(LLMCoreConfig(project="tenant")):
        assert _transport.tenant_user(_attribution_attempt(_attribution_request())) == "tenant"
        assert (
            _transport.tenant_user(
                _attribution_attempt(_attribution_request(tenant=TenantSpec(workload_stage="stage")))
            )
            == "tenant/stage"
        )
        assert (
            _transport.tenant_user(_attribution_attempt(_attribution_request(tenant=TenantSpec(run_id="run"))))
            == "tenant//run"
        )


def test_tenant_user_strips_controls_but_preserves_printable_unicode() -> None:
    request = _attribution_request(
        tenant=TenantSpec(project="café\nproject", workload_stage="étude\rstage\x85", run_id="run")
    )

    assert _transport.tenant_user(_attribution_attempt(request)) == "caféproject/étudestage/run"


def test_call_context_links_retries_and_fallbacks_with_one_logical_request_id() -> None:
    primary = DEFAULT_TEST_MODEL.model_copy(update={"fallback": ALTERNATE_TEST_MODEL})
    ctx = _CallContext(replace(_attribution_request(), model=primary))

    first = ctx.build_attempt(primary, 0)
    ctx.absorb(
        AttemptOutcome(
            error=RuntimeError("rate limited"),
            headers=_aipl.AIPLResponseHeaders(deployment_id="deployment-primary", failure_class="rate"),
            failed_deployment_id="deployment-primary",
        ),
        first,
    )
    retry = ctx.build_attempt(primary, 1)
    ctx.absorb(AttemptOutcome(error=RuntimeError("server"), failed_deployment_id="deployment-primary-2"), retry)
    fallback = ctx.build_attempt(ALTERNATE_TEST_MODEL, 0)

    assert first.correlation is not None
    assert retry.correlation is not None
    assert fallback.correlation is not None
    assert retry.correlation.logical_request_id == first.correlation.logical_request_id
    assert retry.correlation.attempt_number == 2
    assert retry.correlation.attempt_kind == "retry_same_model"
    assert retry.correlation.is_fallback is False
    assert retry.correlation.prev_call_id == first.call_id
    assert retry.correlation.prev_deployment_id == "deployment-primary"
    assert retry.correlation.prev_failure_class == "rate"
    assert fallback.correlation.logical_request_id == first.correlation.logical_request_id
    assert fallback.correlation.attempt_number == 3
    assert fallback.correlation.attempt_kind == "fallback_model"
    assert fallback.correlation.is_fallback is True
    assert fallback.correlation.prev_call_id == retry.call_id
    assert fallback.correlation.prev_deployment_id == "deployment-primary-2"


def test_classify_stream_watchdog_returns_watchdog() -> None:
    exc = StreamWatchdogError("stall", "deployment-A", observed_tps=2.0)

    assert _aipl.classify(exc) == "watchdog"


def test_classify_empty_response_error_returns_upstream_empty_stream() -> None:
    """``EmptyResponseError`` must route through the upstream-empty-stream class.

    The proxy cannot detect emptiness when it returns 200 with content that
    parses to no usable output (blank string, empty JSON fence, null choice).
    The framework classification covers that gap so the failed deployment is
    skipped on the next attempt.
    """
    exc = EmptyResponseError("blank", deployment_id="deployment-A")

    assert _aipl.classify(exc) == "upstream_empty_stream"


def test_classify_midstream_provider_error_returns_midstream_failure() -> None:
    exc = MidStreamProviderError("stream dropped after partial output", deployment_id="deployment-A")

    assert _aipl.classify(exc) == "midstream_failure"


def test_classify_partial_tool_call_stream_error_returns_midstream_failure() -> None:
    exc = PartialToolCallStreamError("incomplete tool call", deployment_id="deployment-A")

    assert _aipl.classify(exc) == "midstream_failure"


def test_classify_content_policy_error_returns_refusal() -> None:
    """``finish_reason=content_filter`` raises ContentPolicyError; it must classify as refusal.

    Different providers have different content policies, so refusal advances
    the AIModel chain rather than burning retries on sibling deployments of
    the same model.
    """
    exc = ContentPolicyError("blocked", deployment_id="deployment-A")

    assert _aipl.classify(exc) == "refusal"


def test_classify_prefers_proxy_failure_class_header() -> None:
    headers = _aipl.AIPLResponseHeaders.from_response_headers({
        "x-aipl-deployment-id": "deployment-A",
        "x-aipl-failure-class": "context_limit",
    })

    assert _aipl.classify(RuntimeError("ignored"), headers=headers) == "context_limit"


def test_classify_ignores_unknown_proxy_failure_class() -> None:
    headers = _aipl.AIPLResponseHeaders.from_response_headers({
        "x-aipl-deployment-id": "deployment-A",
        "x-aipl-failure-class": "some_new_thing",
    })

    assert _aipl.classify(RuntimeError("ignored"), headers=headers) is None


def test_classify_unknown_proxy_class_logs_warning_and_falls_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unknown proxy failure_class logs a WARNING and falls back to local logic."""
    headers = _aipl.AIPLResponseHeaders.from_response_headers({
        "x-aipl-deployment-id": "deployment-A",
        "x-aipl-failure-class": "quota_exhausted",
    })
    exc = StreamWatchdogError("stall", "deployment-A", observed_tps=1.0)

    with caplog.at_level(logging.WARNING, logger="ai_pipeline_core._llm_core._aipl"):
        result = _aipl.classify(exc, headers=headers)

    assert result == "watchdog"  # falls back to local exception-based classification
    assert "quota_exhausted" in caplog.text
    assert "Unknown AIPL failure_class" in caplog.text


def test_known_failure_classes_populated_at_module_load() -> None:
    """Sanity check that ``_KNOWN_FAILURE_CLASSES`` resolved from the Literal.

    Defends against a future Python typing change silently returning an empty
    tuple from ``get_args(FailureClass.__value__)`` — which would make every
    proxy-stamped failure_class fall through to local classification.
    """
    known = _aipl._KNOWN_FAILURE_CLASSES
    assert "context_limit" in known
    assert "capability_mismatch" in known
    assert "refusal" in known
    assert "watchdog" in known
    assert "client_aborted_stream" in known
    assert "upstream_empty_stream" in known
    assert "midstream_failure" in known
    assert len(known) >= 13


def test_route_watchdog_skips_deployment_and_backs_off_workload() -> None:
    action = _aipl.route("watchdog")

    assert action.skip_deployment is True
    assert action.backoff_workload is True
    assert action.terminal_for_model is False


@pytest.mark.parametrize("failure_class", ["context_limit", "capability_mismatch", "refusal"])
def test_route_terminal_classes_advance_model_chain(failure_class: Any) -> None:
    action = _aipl.route(failure_class)

    assert action.terminal_for_model is True
    assert action.skip_deployment is False


@pytest.mark.parametrize("failure_class", ["client_aborted_stream", "upstream_empty_stream", "midstream_failure"])
def test_route_proxy_stream_failures_skip_and_backoff(failure_class: Any) -> None:
    action = _aipl.route(failure_class)

    assert action.skip_deployment is True
    assert action.backoff_workload is True
    assert action.terminal_for_model is False


def test_aipl_info_cache_properties_from_cc_dedup() -> None:
    """``cc_dedup`` maps cleanly into the two explicit-cache properties."""
    assert AIPLInfo(cc_dedup="owner_published").explicit_cache_created is True
    assert AIPLInfo(cc_dedup="owner_published").explicit_cache_used is False
    assert AIPLInfo(cc_dedup="redis_done_hit").explicit_cache_used is True
    assert AIPLInfo(cc_dedup="redis_done_hit").explicit_cache_created is False
    assert AIPLInfo(cc_dedup="redis_done_miss").explicit_cache_used is False
    assert AIPLInfo(cc_dedup="redis_done_miss").explicit_cache_created is False
    assert AIPLInfo(cc_dedup=None).explicit_cache_used is False
    assert AIPLInfo(cc_dedup=None).explicit_cache_created is False


def test_aipl_response_headers_parses_failure_class_and_workload_kind() -> None:
    """Proxy stamps survive header parsing and reach the typed dataclass."""
    headers = _aipl.AIPLResponseHeaders.from_response_headers({
        "x-aipl-deployment-id": "deployment-A",
        "x-aipl-failure-class": "refusal",
        "x-aipl-workload-kind": "summarize",
    })

    assert headers.failure_class == "refusal"
    assert headers.workload_kind == "summarize"


@pytest.mark.asyncio
async def test_maybe_fetch_trace_suppresses_recovered_success(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fake_fetch_trace(call_id: str | None) -> dict[str, Any] | None:
        _ = call_id
        return {"result": "success", "attempts": [{"exception_class": "RateLimitError"}, {"result": "success"}]}

    monkeypatch.setattr(_aipl, "fetch_trace", fake_fetch_trace)
    with caplog.at_level(logging.WARNING, logger="ai_pipeline_core._llm_core._aipl"):
        await _aipl.maybe_fetch_trace(RuntimeError("transient"), call_id="call-xyz")

    assert caplog.text == ""


@pytest.mark.asyncio
async def test_maybe_fetch_trace_labels_with_trace_exception_class(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fake_fetch_trace(call_id: str | None) -> dict[str, Any] | None:
        _ = call_id
        return {"result": "failed", "attempts": [{"exception_class": "EmptyUpstreamResponse"}]}

    monkeypatch.setattr(_aipl, "fetch_trace", fake_fetch_trace)
    with caplog.at_level(logging.WARNING, logger="ai_pipeline_core._llm_core._aipl"):
        await _aipl.maybe_fetch_trace(RuntimeError("framework wrapper"), call_id="call-xyz")

    assert "EmptyUpstreamResponse" in caplog.text
    assert "RuntimeError" not in caplog.text


@pytest.mark.asyncio
async def test_maybe_fetch_trace_label_uses_last_failed_attempt_when_last_has_no_class(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the final attempt lacks an exception class, walk back to the last failed one."""

    async def fake_fetch_trace(call_id: str | None) -> dict[str, Any] | None:
        _ = call_id
        return {
            "result": "failed",
            "attempts": [
                {"exception_class": "EmptyUpstreamResponse", "result": "failed"},
                {"result": "failed"},
            ],
        }

    monkeypatch.setattr(_aipl, "fetch_trace", fake_fetch_trace)
    with caplog.at_level(logging.WARNING, logger="ai_pipeline_core._llm_core._aipl"):
        await _aipl.maybe_fetch_trace(RuntimeError("wrapper"), call_id="call-xyz")

    assert "EmptyUpstreamResponse" in caplog.text


@pytest.mark.asyncio
async def test_maybe_fetch_trace_label_falls_back_to_failure_class(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When no exception_class is present, the failure_class still wins over the wrapper."""

    async def fake_fetch_trace(call_id: str | None) -> dict[str, Any] | None:
        _ = call_id
        return {"result": "failed", "attempts": [{"failure_class": "upstream_empty_stream"}]}

    monkeypatch.setattr(_aipl, "fetch_trace", fake_fetch_trace)
    with caplog.at_level(logging.WARNING, logger="ai_pipeline_core._llm_core._aipl"):
        await _aipl.maybe_fetch_trace(RuntimeError("wrapper"), call_id="call-xyz")

    assert "upstream_empty_stream" in caplog.text


def test_headers_from_exception_walks_cause_chain() -> None:
    """``TerminalError raise ... from exc`` exposes headers through ``__cause__``."""

    class _Resp:
        headers = {"x-aipl-deployment-id": "dep-cause", "x-aipl-failure-class": "context_limit"}

    cause = RuntimeError("upstream")
    cause.response = _Resp()  # type: ignore[attr-defined]  # mimics openai SDK shape
    try:
        try:
            raise cause
        except RuntimeError as exc:
            raise ValueError("wrapper") from exc
    except ValueError as wrapper:
        headers = _aipl.headers_from_exception(wrapper)

    assert headers is not None
    assert headers.deployment_id == "dep-cause"
    assert headers.failure_class == "context_limit"


def test_headers_from_exception_reads_direct_response_headers() -> None:
    """Exceptions carrying ``response_headers`` directly are read without walking the chain."""
    exc = EmptyResponseError(
        "blank",
        deployment_id="dep-direct",
        response_headers={"x-aipl-deployment-id": "dep-direct", "x-aipl-provider": "openrouter"},
    )

    headers = _aipl.headers_from_exception(exc)

    assert headers is not None
    assert headers.deployment_id == "dep-direct"
    assert headers.provider == "openrouter"


def test_headers_from_exception_returns_none_when_unknown() -> None:
    assert _aipl.headers_from_exception(RuntimeError("no headers anywhere")) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403, 404])
async def test_fetch_trace_logs_non_200(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    status_code: int,
) -> None:
    class TraceClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> TraceClient:
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
            _ = (exc_type, exc, traceback)

        async def get(self, url: str, *, headers: dict[str, str]) -> httpx.Response:
            _ = headers
            return httpx.Response(status_code, request=httpx.Request("GET", url))

    from ai_pipeline_core._llm_core._config import LLMCoreConfig, override_config

    monkeypatch.setattr(_aipl.httpx, "AsyncClient", TraceClient)
    config = LLMCoreConfig(openai_api_key="key", openai_base_url="http://proxy/v1")
    with override_config(config), caplog.at_level(logging.DEBUG, logger="ai_pipeline_core._llm_core._aipl"):
        trace = await _aipl.fetch_trace("call-123")

    assert trace is None
    assert f"status={status_code}" in caplog.text
    assert "http://proxy/aipl/trace/call-123" in caplog.text


@pytest.mark.asyncio
async def test_fetch_trace_url_encodes_proxy_supplied_call_id(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_urls: list[str] = []

    class TraceClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> TraceClient:
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
            _ = (exc_type, exc, traceback)

        async def get(self, url: str, *, headers: dict[str, str]) -> httpx.Response:
            _ = headers
            captured_urls.append(url)
            return httpx.Response(200, json={}, request=httpx.Request("GET", url))

    monkeypatch.setattr(_aipl.httpx, "AsyncClient", TraceClient)
    config = LLMCoreConfig(openai_api_key="key", openai_base_url="http://proxy/v1")
    with override_config(config):
        trace = await _aipl.fetch_trace("call/../x?debug=true#frag")

    assert trace == {}
    assert captured_urls == ["http://proxy/aipl/trace/call%2F..%2Fx%3Fdebug%3Dtrue%23frag"]
