"""Parsed AIPL response headers extracted into a leaf module.

Lives separately from ``_aipl`` so ``model_response.py`` can name it without
forming a cycle (``_aipl`` imports ``model_response`` for ``StreamCompletion``).
``_aipl`` re-exports ``AIPLResponseHeaders`` for backwards compatibility.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from .exceptions import AIPLHeaderParseError

__all__ = ["AIPLResponseHeaders", "GroupStatus"]


type GroupStatus = Literal["ok", "degraded", "exhausted"]


@dataclass(frozen=True, slots=True)
class AIPLResponseHeaders:
    """Parsed AIPL and LiteLLM response headers."""

    call_id: str | None = None
    deployment_id: str | None = None
    provider: str | None = None
    openrouter_provider: str | None = None
    openrouter_upstream: str | None = None
    openrouter_generation_id: str | None = None
    response_cache_hit: bool = False
    tried_deployments: tuple[str, ...] = ()
    failed_deployments: tuple[str, ...] = ()
    group_status: GroupStatus | None = None
    warmup_strategy: str | None = None
    warmup_role: str | None = None
    warmup_wait_ms: int | None = None
    limit_wait_ms: int | None = None
    limit_denied: bool = False
    limit_denied_reason: str | None = None
    response_format_downgraded: bool = False
    cost_optimized_skipped: bool = False
    cc_dedup: str | None = None
    cc_stale: bool = False
    failure_class: str | None = None
    workload_kind: str | None = None
    litellm_call_id: str | None = None
    litellm_attempted_retries: int | None = None
    litellm_attempted_fallbacks: int | None = None
    response_cost: float | None = None
    retry_after_s: float | None = None

    @classmethod
    def from_response_headers(cls, headers: Mapping[str, str] | None) -> AIPLResponseHeaders:
        """Parse AIPL proxy headers into a typed intermediate."""
        if not headers:
            return cls()
        lower = {str(key).lower(): str(value) for key, value in headers.items()}
        limit_denied_reason = lower.get("x-aipl-limit-denied") or None
        return cls(
            call_id=lower.get("x-aipl-call-id"),
            deployment_id=lower.get("x-aipl-deployment-id") or lower.get("x-litellm-model-id"),
            provider=lower.get("x-aipl-provider"),
            openrouter_provider=lower.get("x-aipl-openrouter-provider"),
            openrouter_upstream=lower.get("x-aipl-openrouter-upstream"),
            openrouter_generation_id=lower.get("x-aipl-openrouter-generation-id"),
            response_cache_hit=_parse_bool_flag(lower.get("x-aipl-response-cache-hit"), "x-aipl-response-cache-hit"),
            tried_deployments=_parse_csv(lower.get("x-aipl-tried-deployments")),
            failed_deployments=_parse_csv(lower.get("x-aipl-failed-deployments")),
            group_status=_parse_group_status(lower.get("x-aipl-group-status")),
            warmup_strategy=lower.get("x-aipl-warmup-strategy"),
            warmup_role=lower.get("x-aipl-warmup-role"),
            warmup_wait_ms=_parse_int(lower.get("x-aipl-warmup-wait-ms"), "x-aipl-warmup-wait-ms"),
            limit_wait_ms=_parse_int(lower.get("x-aipl-limit-wait-ms"), "x-aipl-limit-wait-ms"),
            limit_denied=bool(limit_denied_reason),
            limit_denied_reason=limit_denied_reason,
            response_format_downgraded=_parse_bool_flag(
                lower.get("x-aipl-response-format-downgraded"), "x-aipl-response-format-downgraded"
            ),
            cost_optimized_skipped=_parse_bool_flag(
                lower.get("x-aipl-cost-optimized-skipped"), "x-aipl-cost-optimized-skipped"
            ),
            cc_dedup=lower.get("x-aipl-cc-dedup"),
            cc_stale=_parse_bool_flag(lower.get("x-aipl-cc-stale"), "x-aipl-cc-stale"),
            failure_class=lower.get("x-aipl-failure-class"),
            workload_kind=lower.get("x-aipl-workload-kind"),
            litellm_call_id=lower.get("x-litellm-call-id"),
            litellm_attempted_retries=_parse_int(
                lower.get("x-litellm-attempted-retries"), "x-litellm-attempted-retries"
            ),
            litellm_attempted_fallbacks=_parse_int(
                lower.get("x-litellm-attempted-fallbacks"), "x-litellm-attempted-fallbacks"
            ),
            response_cost=_parse_float(lower.get("x-litellm-response-cost"), "x-litellm-response-cost"),
            retry_after_s=_parse_retry_after(lower.get("retry-after")),
        )


def _parse_csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _parse_int(value: str | None, header: str) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        raise AIPLHeaderParseError(f"Malformed integer AIPL header {header}: {value!r}") from None


def _parse_float(value: str | None, header: str) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        raise AIPLHeaderParseError(f"Malformed float AIPL header {header}: {value!r}") from None


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a standard ``Retry-After`` header in delta-seconds form.

    Unlike the strict AIPL parsers, this never raises: ``Retry-After`` may
    legitimately be an HTTP-date, which we do not honor (return ``None`` so the
    caller falls back to its computed backoff) rather than failing the request.
    """
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _parse_bool_flag(value: str | None, header: str) -> bool:
    if not value:
        return False
    normalized = value.strip().lower()
    if normalized in {"1", "true"}:
        return True
    if normalized in {"0", "false"}:
        return False
    raise AIPLHeaderParseError(f"Malformed boolean AIPL header {header}: {value!r}")


def _parse_group_status(value: str | None) -> GroupStatus | None:
    if value == "ok":
        return "ok"
    if value == "degraded":
        return "degraded"
    if value == "exhausted":
        return "exhausted"
    if value:
        raise AIPLHeaderParseError(f"Malformed AIPL group status header: {value!r}")
    return None
