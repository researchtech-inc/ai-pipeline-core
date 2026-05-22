"""AIPL proxy protocol parsing, routing hints, and metadata construction."""

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

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
from .exceptions import ContentPolicyError, StreamWatchdogError
from .model_response import StreamCompletion
from .request import AttemptRequest

__all__ = [
    "AIPLResponseHeaders",
    "FailureAction",
    "FailureClass",
    "build_aipl_metadata",
    "build_transport_metadata",
    "classify",
    "deployment_id_from",
    "fetch_trace",
    "is_group_exhausted",
    "maybe_fetch_trace",
    "route",
]

logger = logging.getLogger(__name__)
_TRACE_ERROR_BODY_PREVIEW_CHARS = 200

type FailureClass = Literal["auth", "policy", "server", "rate", "timeout", "limiter"]


@dataclass(frozen=True, slots=True)
class FailureAction:
    """Routing action for one classified failure."""

    skip_deployment: bool
    backoff_workload: bool


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


def classify(exc: BaseException, *, headers: AIPLResponseHeaders | None = None) -> FailureClass | None:
    """Classify an attempt failure using AIPL headers and exception hints."""
    if headers is not None and headers.limit_denied:
        return "limiter"
    if isinstance(exc, ContentPolicyError):
        return "policy"
    if isinstance(exc, StreamWatchdogError):
        return "timeout"
    status = _extract_status(exc)
    status_map: dict[int, FailureClass] = {
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
    return status_map.get(status) if status is not None else None


def route(failure_class: FailureClass | None) -> FailureAction:
    """Map a failure class to routing actions."""
    if failure_class == "limiter":
        return FailureAction(skip_deployment=True, backoff_workload=True)
    if failure_class in {"auth", "policy"}:
        return FailureAction(skip_deployment=True, backoff_workload=False)
    if failure_class in {"server", "rate", "timeout"}:
        return FailureAction(skip_deployment=False, backoff_workload=failure_class == "timeout")
    return FailureAction(skip_deployment=False, backoff_workload=False)


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
    """Fetch and log the AIPL trace blob on failures."""
    if not call_id:
        return
    trace = await fetch_trace(call_id)
    if trace is None:
        return
    logger.warning(
        "AIPL trace for failed call %s (%s): %s",
        call_id,
        type(exc).__name__,
        json.dumps(trace, default=str, sort_keys=True),
    )


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
