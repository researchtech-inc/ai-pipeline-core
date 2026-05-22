"""Typed request objects for the LLM execution path."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel

from ._defaults import DEFAULT_CACHE_TTL_MINUTES, cache_ttl_to_wire
from .types import AIModel, CoreMessage

__all__ = [
    "AttemptRequest",
    "CacheSpec",
    "DebugSpec",
    "GenerationSpec",
    "LLMRequest",
    "ListOf",
    "ResponseSpec",
    "RetrySpec",
    "RoutingSpec",
    "ToolSpec",
    "ValidationSpec",
    "get_list_item_type",
    "is_list_output_type",
]


@dataclass(frozen=True, slots=True)
class GenerationSpec:
    """LLM generation parameters."""

    temperature: float | None = None
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh"] | None = None
    verbosity: Literal["low", "medium", "high"] | None = None
    max_completion_tokens: int | None = None
    stop: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class CacheSpec:
    """Prompt cache marker and response-cache configuration.

    ``ttl`` is integer minutes: ``0`` disables explicit cache markers (the
    provider may still apply implicit caching when available), and ``1..1440``
    are wire-converted to the seconds form required upstream. The
    ``prompt_cache_key`` is computed and sent independently of ``ttl`` whenever
    ``context_count > 0``.
    """

    context_count: int = 0
    ttl: int = DEFAULT_CACHE_TTL_MINUTES
    key_override: str | None = None
    bypass_response_cache: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.context_count, bool) or not isinstance(self.context_count, int):
            raise TypeError(f"CacheSpec.context_count must be int; got {type(self.context_count).__name__}.")
        if self.context_count < 0:
            raise ValueError(f"CacheSpec.context_count must be >= 0; got {self.context_count}.")
        cache_ttl_to_wire(self.ttl)


@dataclass(frozen=True, slots=True)
class RoutingSpec:
    """Routing hints consumed by the AIPL proxy."""

    workload_id: str | None = None
    force_deployment_id: str | None = None
    preferred_deployment_id: str | None = None
    skip_cost_optimized: bool | None = None
    skip_ids: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ListOf:
    """Structured-output sentinel for list responses."""

    inner: type[BaseModel]


def is_list_output_type(output_type: Any) -> bool:
    """Return True when an annotation is ``list[BaseModelSubclass]``."""
    if get_origin(output_type) is not list:
        return False
    args = get_args(output_type)
    return bool(args) and isinstance(args[0], type) and issubclass(args[0], BaseModel)


def get_list_item_type(output_type: Any) -> type[BaseModel]:
    """Extract ``T`` from a previously validated ``list[T]`` annotation."""
    return get_args(output_type)[0]


@dataclass(frozen=True, slots=True)
class ResponseSpec:
    """Structured response declaration."""

    format: type[BaseModel] | ListOf | None = None


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Transport-layer tool schema declaration."""

    schemas: tuple[dict[str, Any], ...] = ()
    choice: str | dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class DebugSpec:
    """Diagnostic overrides for one request."""

    capture_trace: bool = False
    disable_watchdog: bool = False
    warmup_strategy_override: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class RetrySpec:
    """Retry and timeout policy."""

    retries: int | None = None
    retry_delay_seconds: float | None = None
    timeout_s: float | None = None
    min_output_tps: float | None = None


@dataclass(frozen=True, slots=True)
class ValidationSpec:
    """Outer repair-loop policy for structured-output validation.

    ``validate`` is invoked on the engine's parsed response (typed as ``Any``
    so contract subclasses can narrow the parameter type without violating
    callable contravariance). It must return a sequence of failure items;
    each item is duck-typed with optional ``field`` (str | None) and
    ``message`` (str) attributes that the engine renders into a USER repair
    message. An empty sequence signals success.

    ``max_attempts`` caps the total engine invocations (1 = no repair, 2 = one
    repair, etc.). Exhaustion raises ``TerminalError`` from the engine.
    """

    validate: Callable[[Any], Sequence[Any]]
    max_attempts: int = 1

    def __post_init__(self) -> None:
        if not callable(self.validate):
            raise TypeError(f"ValidationSpec.validate must be callable; got {type(self.validate).__name__}.")
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int):
            raise TypeError(f"ValidationSpec.max_attempts must be int; got {type(self.max_attempts).__name__}.")
        if self.max_attempts < 1:
            raise ValueError(f"ValidationSpec.max_attempts must be >= 1; got {self.max_attempts}.")


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """Input to one logical LLM generation call."""

    model: AIModel
    messages: tuple[CoreMessage, ...]
    generation: GenerationSpec = field(default_factory=GenerationSpec)
    cache: CacheSpec = field(default_factory=CacheSpec)
    routing: RoutingSpec = field(default_factory=RoutingSpec)
    response: ResponseSpec = field(default_factory=ResponseSpec)
    tools: ToolSpec = field(default_factory=ToolSpec)
    debug: DebugSpec = field(default_factory=DebugSpec)
    retry: RetrySpec = field(default_factory=RetrySpec)
    purpose: str | None = None


@dataclass(frozen=True, slots=True)
class AttemptRequest:
    """One HTTP attempt against one AIModel hop."""

    call: LLMRequest
    model: AIModel
    attempt_index: int
    call_id: str
