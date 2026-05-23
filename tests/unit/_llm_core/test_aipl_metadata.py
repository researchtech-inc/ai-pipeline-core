"""AIPL metadata and trace helper tests."""

import logging
from typing import Any

import httpx
import pytest

from ai_pipeline_core._llm_core import _aipl
from ai_pipeline_core._llm_core._transport_metadata import AIPLInfo
from ai_pipeline_core._llm_core.exceptions import (
    ContentPolicyError,
    EmptyResponseError,
    MidStreamProviderError,
    PartialToolCallStreamError,
    StreamWatchdogError,
)
from ai_pipeline_core._llm_core.request import AttemptRequest, LLMRequest
from ai_pipeline_core._llm_core.types import CoreMessage, Role
from tests.support.model_catalog import DEFAULT_TEST_MODEL


def _attempt() -> AttemptRequest:
    request = LLMRequest(
        model=DEFAULT_TEST_MODEL,
        messages=(CoreMessage(role=Role.USER, content="hi"),),
    )
    return AttemptRequest(call=request, model=request.model, attempt_index=0, call_id="call-123")


def test_build_aipl_metadata_includes_aipl_call_id() -> None:
    metadata = _aipl.build_aipl_metadata(_attempt())

    assert metadata == {"aipl": "1", "aipl_call_id": "call-123"}


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
