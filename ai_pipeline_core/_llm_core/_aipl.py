"""AIPL proxy protocol parsing, routing hints, and metadata construction."""

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast, get_args

import httpx

from ._aipl_headers import AIPLResponseHeaders
from ._config import get_config
from ._transport_metadata import (
    AIPLInfo,
    LimiterInfo,
    LiteLLMInfo,
    ModelChainInfo,
    OpenRouterInfo,
    TransportMetadata,
    TransportTiming,
    WarmupInfo,
)
from .exceptions import (
    ContentPolicyError,
    EmptyResponseError,
    MidStreamProviderError,
    PartialToolCallStreamError,
    StreamWatchdogError,
    StructuredRepairExhaustedError,
)
from .model_response import StreamCompletion
from .request import AttemptRequest

__all__ = [
    "AIPLResponseHeaders",
    "FailureAction",
    "FailureClass",
    "aipl_attribution_from_exception",
    "build_aipl_metadata",
    "build_transport_metadata",
    "classify",
    "deployment_id_from",
    "fetch_trace",
    "headers_from_exception",
    "is_group_exhausted",
    "maybe_fetch_trace",
    "route",
]

logger = logging.getLogger(__name__)
_TRACE_ERROR_BODY_PREVIEW_CHARS = 200

type FailureClass = Literal[
    "auth",
    "policy",
    "server",
    "rate",
    "timeout",
    "limiter",
    "watchdog",
    "context_limit",
    "capability_mismatch",
    "refusal",
    "client_aborted_stream",
    "upstream_empty_stream",
    "midstream_failure",
]
_KNOWN_FAILURE_CLASSES: frozenset[str] = frozenset(get_args(FailureClass.__value__))


@dataclass(frozen=True, slots=True)
class FailureAction:
    """Routing action for one classified failure.

    ``terminal_for_model`` advances the AIModel chain immediately after a
    single failure. ``advance_after_exhaustion`` advances only when the
    framework has burned the per-model retry budget without producing any
    response — failures classified this way are "transient" (slow deployment,
    server hiccup) and switching to a different model is the right next step.
    Failures with neither flag (e.g. ``auth`` / ``policy``) keep retrying
    within the model_group and never escalate, because a misconfigured
    credential or content policy will not be fixed by trying a sibling model.
    """

    skip_deployment: bool = False
    backoff_workload: bool = False
    terminal_for_model: bool = False
    advance_after_exhaustion: bool = False


def build_aipl_metadata(req: AttemptRequest) -> dict[str, Any]:
    """Build LiteLLM metadata carrying AIPL routing signals."""
    metadata: dict[str, Any] = {"aipl": "1", "aipl_call_id": req.call_id}
    if req.call.routing.skip_ids:
        metadata["aipl_skip_model_ids"] = sorted(req.call.routing.skip_ids)
    if req.call.routing.force_deployment_id:
        metadata["aipl_force_deployment_id"] = req.call.routing.force_deployment_id
    if req.call.routing.preferred_deployment_id:
        metadata["aipl_prefer_deployment_id"] = req.call.routing.preferred_deployment_id
    if req.call.routing.skip_cost_optimized:
        metadata["aipl_skip_cost_optimized"] = True
    if req.call.debug.warmup_strategy_override:
        metadata["aipl_warmup_strategy_override"] = req.call.debug.warmup_strategy_override
    return metadata


_EXCEPTION_FAILURE_CLASSES: tuple[tuple[type[BaseException], FailureClass], ...] = (
    (ContentPolicyError, "refusal"),
    (StreamWatchdogError, "watchdog"),
    (EmptyResponseError, "upstream_empty_stream"),
    (MidStreamProviderError, "midstream_failure"),
    (PartialToolCallStreamError, "midstream_failure"),
    (StructuredRepairExhaustedError, "capability_mismatch"),
)

_STATUS_FAILURE_CLASSES: dict[int, FailureClass] = {
    401: "auth",
    403: "policy",
    404: "auth",
    408: "timeout",
    429: "rate",
    500: "server",
    502: "server",
    503: "server",
    504: "timeout",
    529: "server",
}


def classify(exc: BaseException, *, headers: AIPLResponseHeaders | None = None) -> FailureClass | None:
    """Classify an attempt failure using AIPL headers and exception hints.

    Prefers the proxy-stamped ``x-aipl-failure-class`` header when the value
    is one we know how to route. Falls back to exception-based classification
    so failures without a header (network errors, framework-side aborts) still
    classify correctly.
    """
    if headers is not None and headers.failure_class:
        if headers.failure_class in _KNOWN_FAILURE_CLASSES:
            return cast(FailureClass, headers.failure_class)
        logger.warning(
            "Unknown AIPL failure_class %r from proxy; falling back to exception-based classification. "
            "Add the new class to FailureClass in _llm_core/_aipl.py or upgrade the framework to match.",
            headers.failure_class,
        )
    if headers is not None and headers.limit_denied:
        return "limiter"
    for exception_type, failure_class in _EXCEPTION_FAILURE_CLASSES:
        if isinstance(exc, exception_type):
            return failure_class
    status = _extract_status(exc)
    return _STATUS_FAILURE_CLASSES.get(status) if status is not None else None


def route(failure_class: FailureClass | None) -> FailureAction:
    """Map a failure class to routing actions."""
    if failure_class == "limiter":
        return FailureAction(skip_deployment=True, backoff_workload=True, advance_after_exhaustion=True)
    if failure_class in {"auth", "policy"}:
        return FailureAction(skip_deployment=True)
    if failure_class in {"watchdog", "client_aborted_stream", "upstream_empty_stream", "midstream_failure"}:
        return FailureAction(skip_deployment=True, backoff_workload=True, advance_after_exhaustion=True)
    if failure_class in {"context_limit", "capability_mismatch", "refusal"}:
        return FailureAction(terminal_for_model=True)
    if failure_class in {"server", "rate", "timeout"}:
        return FailureAction(
            backoff_workload=failure_class == "timeout",
            advance_after_exhaustion=True,
        )
    return FailureAction()


def deployment_id_from(value: Any) -> str | None:
    """Best-effort extraction of the deployment id from common framework values."""
    if isinstance(value, AIPLResponseHeaders):
        return value.deployment_id
    direct = getattr(value, "deployment_id", None)
    if isinstance(direct, str):
        return direct
    headers = getattr(value, "headers", None)
    if isinstance(headers, Mapping):
        normalized = {str(key): str(header_value) for key, header_value in headers.items()}
        return AIPLResponseHeaders.from_response_headers(normalized).deployment_id
    transport = getattr(value, "transport", None)
    aipl = getattr(transport, "aipl", None)
    deployment_id = getattr(aipl, "deployment_id", None)
    return deployment_id if isinstance(deployment_id, str) else None


def is_group_exhausted(headers: AIPLResponseHeaders) -> bool:
    """Return True when the proxy asks the caller to advance to fallback."""
    return headers.group_status == "exhausted"


def build_transport_metadata(
    completion: StreamCompletion,
    req: AttemptRequest,
    *,
    prompt_cache_key: str | None,
    response_cost: float | None = None,
) -> TransportMetadata:
    """Build first-class transport metadata from one stream completion."""
    h = completion.aipl_headers
    return TransportMetadata(
        aipl=AIPLInfo(
            call_id=h.call_id,
            deployment_id=h.deployment_id,
            provider=h.provider,
            warmup=WarmupInfo(role=h.warmup_role, strategy=h.warmup_strategy, wait_ms=h.warmup_wait_ms),
            limiter=LimiterInfo(wait_ms=h.limit_wait_ms, denied=h.limit_denied, denied_reason=h.limit_denied_reason),
            group_status=h.group_status,
            tried_deployments=h.tried_deployments,
            failed_deployments=h.failed_deployments,
            response_cache_hit=h.response_cache_hit,
            response_format_downgraded=h.response_format_downgraded,
            cost_optimized_skipped=h.cost_optimized_skipped,
            cc_dedup=h.cc_dedup,
            cc_stale=h.cc_stale,
            failure_class=h.failure_class,
            workload_kind=h.workload_kind,
            openrouter=OpenRouterInfo(
                provider=h.openrouter_provider,
                upstream=h.openrouter_upstream,
                generation_id=h.openrouter_generation_id,
            ),
        ),
        litellm=LiteLLMInfo(
            call_id=h.litellm_call_id,
            attempted_retries=h.litellm_attempted_retries,
            attempted_fallbacks=h.litellm_attempted_fallbacks,
            response_cost=response_cost if response_cost is not None else h.response_cost,
        ),
        timing=TransportTiming(
            time_taken_s=completion.timing.time_taken_s,
            first_token_s=completion.timing.first_token_time_s,
        ),
        model_chain=ModelChainInfo(
            requested=req.call.model.name,
            active=req.model.name,
            used_fallback=req.call.model.name != req.model.name,
        ),
        prompt_cache_key=prompt_cache_key,
        raw_response_headers=tuple(sorted((str(key), str(value)) for key, value in completion.raw_headers.items())),
        final_tps=completion.final_tps,
    )


async def maybe_fetch_trace(exc: BaseException, *, call_id: str) -> None:
    """Fetch and log the AIPL trace blob on failures.

    Suppresses the warning when the proxy considers the call recovered
    (``trace.result == "success"``) so retry-then-success paths stay quiet.
    Labels the log with the proxy-reported ``attempts[-1].exception_class``
    when available, falling back to the framework-side exception name.
    """
    if not call_id:
        return
    trace = await fetch_trace(call_id)
    if trace is None:
        return
    if trace.get("result") == "success":
        return
    logger.warning(
        "AIPL trace for failed call %s (%s): %s",
        call_id,
        _trace_failure_label(trace) or type(exc).__name__,
        json.dumps(trace, default=str, sort_keys=True),
    )


def _trace_failure_label(trace: Mapping[str, Any]) -> str | None:
    """Return the most informative failure label from an AIPL trace.

    Walks ``attempts`` from newest to oldest, skipping successful attempts,
    and returns the first ``exception_class`` (or ``failure_class``) found.
    Last-attempt-only would miss useful data when the final attempt lacks a
    class but an earlier failed attempt has one.
    """
    attempts = trace.get("attempts")
    if not isinstance(attempts, list):
        return None
    for attempt in reversed(attempts):
        if not isinstance(attempt, Mapping) or attempt.get("result") == "success":
            continue
        for key in ("exception_class", "failure_class"):
            value = attempt.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def headers_from_exception(exc: BaseException) -> AIPLResponseHeaders | None:
    """Extract AIPL response headers from an exception, walking the cause chain.

    Empty header mappings are skipped: a bare attribute of ``{}`` must not
    shadow real headers further up the chain, and must not mask the
    exception's own ``deployment_id`` during failure attribution.
    """
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        direct = getattr(current, "response_headers", None)
        if isinstance(direct, Mapping) and direct:
            return AIPLResponseHeaders.from_response_headers({str(key): str(value) for key, value in direct.items()})
        response = getattr(current, "response", None)
        indirect = getattr(response, "headers", None) if response is not None else None
        if isinstance(indirect, Mapping) and indirect:
            return AIPLResponseHeaders.from_response_headers({str(key): str(value) for key, value in indirect.items()})
        current = current.__cause__ or current.__context__
    return None


def aipl_attribution_from_exception(exc: BaseException) -> dict[str, Any] | None:
    """Build span-meta AIPL attribution from an exception, or ``None`` when none is available."""
    headers = headers_from_exception(exc)
    if headers is not None:
        return {
            "call_id": headers.call_id,
            "deployment_id": headers.deployment_id,
            "provider": headers.provider,
            "group_status": headers.group_status,
            "failure_class": headers.failure_class,
            "tried_deployments": list(headers.tried_deployments),
            "failed_deployments": list(headers.failed_deployments),
        }
    deployment_id = getattr(exc, "deployment_id", None)
    if isinstance(deployment_id, str) and deployment_id:
        return {"deployment_id": deployment_id}
    return None


async def fetch_trace(call_id: str | None) -> dict[str, Any] | None:
    """Fetch an AIPL trace blob by call id, returning None when unavailable."""
    if not call_id:
        return None
    api_key = get_config().openai_api_key
    proxy_root = _proxy_root()
    if not proxy_root or not api_key:
        return None
    url = f"{proxy_root}/aipl/trace/{call_id}"
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
            if response.status_code == 200:
                trace = response.json()
                return trace if isinstance(trace, dict) else None
            logger.warning(
                "AIPL trace fetch returned non-200 for call_id=%s: status=%d url=%s reason=%s body=%r",
                call_id,
                response.status_code,
                url,
                response.reason_phrase,
                response.text[:_TRACE_ERROR_BODY_PREVIEW_CHARS],
            )
    except (httpx.HTTPError, ValueError) as trace_exc:  # fmt: skip
        logger.debug("AIPL trace fetch failed for call_id=%s: %s", call_id, trace_exc)
    return None


def _proxy_root() -> str | None:
    base = get_config().openai_base_url
    if not base:
        return None
    root = base.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    return root


def _extract_status(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None) if response is not None else None
    if isinstance(status, int):
        return status
    direct = getattr(exc, "status_code", None)
    return direct if isinstance(direct, int) else None
