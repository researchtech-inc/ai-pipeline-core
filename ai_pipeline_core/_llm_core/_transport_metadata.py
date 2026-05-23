"""Typed transport metadata carried by ModelResponse."""

from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "AIPLInfo",
    "LimiterInfo",
    "LiteLLMInfo",
    "ModelChainInfo",
    "OpenRouterInfo",
    "TransportMetadata",
    "TransportTiming",
    "WarmupInfo",
]


@dataclass(frozen=True, slots=True)
class WarmupInfo:
    """AIPL prompt-cache warmup telemetry."""

    role: str | None = None
    strategy: str | None = None
    wait_ms: int | None = None


@dataclass(frozen=True, slots=True)
class LimiterInfo:
    """AIPL limiter telemetry."""

    wait_ms: int | None = None
    denied: bool = False
    denied_reason: str | None = None


@dataclass(frozen=True, slots=True)
class OpenRouterInfo:
    """OpenRouter routing details surfaced by the proxy."""

    provider: str | None = None
    upstream: str | None = None
    generation_id: str | None = None


@dataclass(frozen=True, slots=True)
class AIPLInfo:
    """Typed view of AIPL proxy response headers and optional trace data."""

    call_id: str | None = None
    deployment_id: str | None = None
    provider: str | None = None
    warmup: WarmupInfo = field(default_factory=WarmupInfo)
    limiter: LimiterInfo = field(default_factory=LimiterInfo)
    group_status: Literal["ok", "degraded", "exhausted"] | None = None
    tried_deployments: tuple[str, ...] = ()
    failed_deployments: tuple[str, ...] = ()
    response_cache_hit: bool = False
    response_format_downgraded: bool = False
    cost_optimized_skipped: bool = False
    reasoning_replay_stripped: bool = False
    cc_dedup: str | None = None
    cc_stale: bool = False
    failure_class: str | None = None
    workload_kind: str | None = None
    openrouter: OpenRouterInfo = field(default_factory=OpenRouterInfo)
    trace: dict[str, Any] | None = None

    @property
    def explicit_cache_created(self) -> bool:
        """True when this request created and published a new explicit cachedContents resource."""
        return self.cc_dedup == "owner_published"

    @property
    def explicit_cache_used(self) -> bool:
        """True when this request reused an existing explicit cachedContents resource via proxy dedup."""
        return self.cc_dedup == "redis_done_hit"


@dataclass(frozen=True, slots=True)
class LiteLLMInfo:
    """LiteLLM-native response metadata."""

    call_id: str | None = None
    attempted_retries: int | None = None
    attempted_fallbacks: int | None = None
    response_cost: float | None = None


@dataclass(frozen=True, slots=True)
class TransportTiming:
    """Provider-call timing facts."""

    time_taken_s: float = 0.0
    first_token_s: float | None = None


@dataclass(frozen=True, slots=True)
class ModelChainInfo:
    """Requested model versus active fallback model."""

    requested: str = ""
    active: str = ""
    used_fallback: bool = False


@dataclass(frozen=True, slots=True)
class TransportMetadata:
    """Single typed view of transport-level facts for one LLM call."""

    aipl: AIPLInfo = field(default_factory=AIPLInfo)
    litellm: LiteLLMInfo = field(default_factory=LiteLLMInfo)
    timing: TransportTiming = field(default_factory=TransportTiming)
    model_chain: ModelChainInfo = field(default_factory=ModelChainInfo)
    prompt_cache_key: str | None = None
    raw_response_headers: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    final_tps: float = 0.0
