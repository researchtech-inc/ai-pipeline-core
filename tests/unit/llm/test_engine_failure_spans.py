"""Failure-path span metadata for the LLM engine.

When ``generate()`` raises, the engine must stamp the round span with the
requested model and any AIPL attribution carried on the exception. Without
this, operator-visible spans land with ``model="unknown"`` and an empty AIPL
block — the success-path stamping is skipped and nothing else fills the gap.
"""

from uuid import uuid7

from ai_pipeline_core._llm_core.exceptions import (
    EmptyResponseError,
    LLMError,
    MidStreamProviderError,
    StreamWatchdogError,
    TerminalError,
)
from ai_pipeline_core._llm_core.request import LLMRequest
from ai_pipeline_core._llm_core.types import CoreMessage, Role
from ai_pipeline_core.llm._engine import _record_failed_round_meta
from ai_pipeline_core.pipeline._span_types import SpanContext
from tests.support.model_catalog import DEFAULT_TEST_MODEL


def _request() -> LLMRequest:
    return LLMRequest(model=DEFAULT_TEST_MODEL, messages=(CoreMessage(role=Role.USER, content="hi"),))


def _span() -> SpanContext:
    return SpanContext(span_id=uuid7(), parent_span_id=None)


def test_failed_round_meta_includes_model_when_no_attribution_available() -> None:
    """Failures without AIPL data still get the requested model on the span."""
    ctx = _span()

    _record_failed_round_meta(ctx, LLMError("no headers anywhere"), _request())

    assert ctx._meta["model"] == DEFAULT_TEST_MODEL.name
    assert ctx._meta["requested_model"] == DEFAULT_TEST_MODEL.name
    assert "aipl" not in ctx._meta


def test_failed_round_meta_extracts_aipl_attribution_from_direct_response_headers() -> None:
    """Exceptions that carry ``response_headers`` directly populate the AIPL block."""
    ctx = _span()
    exc = EmptyResponseError(
        "blank",
        deployment_id="dep-empty",
        response_headers={
            "x-aipl-call-id": "call-1",
            "x-aipl-deployment-id": "dep-empty",
            "x-aipl-provider": "openrouter",
            "x-aipl-failure-class": "upstream_empty_stream",
            "x-aipl-group-status": "ok",
        },
    )

    _record_failed_round_meta(ctx, exc, _request())

    assert ctx._meta["model"] == DEFAULT_TEST_MODEL.name
    assert ctx._meta["aipl"] == {
        "call_id": "call-1",
        "deployment_id": "dep-empty",
        "provider": "openrouter",
        "group_status": "ok",
        "failure_class": "upstream_empty_stream",
        "tried_deployments": [],
        "failed_deployments": [],
    }


def test_failed_round_meta_walks_cause_chain_for_attribution() -> None:
    """``TerminalError raise ... from exc`` exposes headers through ``__cause__``."""
    ctx = _span()

    class _Resp:
        headers = {"x-aipl-deployment-id": "dep-cause", "x-aipl-failure-class": "context_limit"}

    cause = RuntimeError("upstream")
    cause.response = _Resp()  # type: ignore[attr-defined]  # mimics openai SDK shape
    try:
        try:
            raise cause
        except RuntimeError as inner:
            raise TerminalError("wrapper") from inner
    except TerminalError as wrapper:
        _record_failed_round_meta(ctx, wrapper, _request())

    assert ctx._meta["aipl"]["deployment_id"] == "dep-cause"
    assert ctx._meta["aipl"]["failure_class"] == "context_limit"


def test_failed_round_meta_falls_back_to_deployment_id_only() -> None:
    """Watchdog kills carry ``deployment_id`` but no headers; that minimum still lands."""
    ctx = _span()
    exc = StreamWatchdogError("ttft", "dep-watchdog", observed_tps=0.0)

    _record_failed_round_meta(ctx, exc, _request())

    assert ctx._meta["aipl"] == {"deployment_id": "dep-watchdog"}


def test_failed_round_meta_records_midstream_failure_attribution() -> None:
    ctx = _span()
    exc = MidStreamProviderError(
        "dropped",
        deployment_id="dep-mid",
        response_headers={
            "x-aipl-deployment-id": "dep-mid",
            "x-aipl-failure-class": "midstream_failure",
        },
    )

    _record_failed_round_meta(ctx, exc, _request())

    assert ctx._meta["aipl"]["deployment_id"] == "dep-mid"
    assert ctx._meta["aipl"]["failure_class"] == "midstream_failure"
