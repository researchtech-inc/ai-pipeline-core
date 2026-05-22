"""Unified ModelResponse and LLM execution result types.

Provides a single generic ModelResponse[T] class for both structured and
unstructured LLM output. Fully serializable with Pydantic.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from ._aipl_headers import AIPLResponseHeaders
from ._transport_metadata import TransportMetadata
from .types import RawToolCall, TokenUsage

T = TypeVar("T", default=str)
"""Type parameter for response output. str for unstructured, BaseModel subclass for structured."""


class Citation(BaseModel):
    """A URL citation returned by search-enabled models.

    The start_index and end_index fields indicate character positions in the response content
    where the citation applies. Note that index behavior varies by model.
    """

    model_config = ConfigDict(frozen=True)

    title: str
    url: str
    start_index: int
    end_index: int


@dataclass(frozen=True, slots=True)
class TimingData:
    """Timing captured while draining one stream."""

    started_at: float
    first_token_at: float | None
    finished_at: float

    @property
    def time_taken_s(self) -> float:
        """Total provider-call duration in seconds."""
        return self.finished_at - self.started_at

    @property
    def first_token_time_s(self) -> float | None:
        """Seconds to first visible token, if any visible token arrived."""
        if self.first_token_at is None:
            return None
        return self.first_token_at - self.started_at


@dataclass(frozen=True, slots=True)
class StreamCompletion:
    """Output of stream consumption before response normalization."""

    response: Any
    usage: Any | None
    raw_headers: Mapping[str, str]
    aipl_headers: AIPLResponseHeaders
    timing: TimingData
    final_tps: float


@dataclass(frozen=True, slots=True)
class AttemptOutcome:
    """Result of one HTTP attempt."""

    response: ModelResponse[Any] | None = None
    error: BaseException | None = None
    new_skip_ids: frozenset[str] = frozenset()
    failed_deployment_id: str | None = None
    headers: AIPLResponseHeaders | None = None
    advance_to_fallback: bool = False
    demote_workload: bool = False


class ModelResponse(BaseModel, Generic[T]):
    """Unified LLM response for both structured and unstructured output.

    Generic[T] provides type hints:
    - ModelResponse[str]: unstructured text output
    - ModelResponse[MyModel]: structured output with typed .parsed

    All fields are serializable. After JSON round-trip, `parsed` becomes a dict
    for structured responses (use MyModel.model_validate(response.parsed) to reconstruct).
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    # Core response data
    content: str
    """Raw LLM output text (or JSON string for structured)."""

    parsed: T
    """Parsed result: same as content for T=str, typed model for T=BaseModel."""

    reasoning_content: str = ""
    """Reasoning/thinking content from the model (if available)."""

    citations: tuple[Citation, ...] = ()
    """URL citations from search-enabled models."""

    # Usage and cost
    usage: TokenUsage
    """Token usage statistics."""

    cost: float | None = None
    """Generation cost in USD (if available)."""

    # Metadata for observability
    model: str
    """Model identifier used for generation."""

    response_id: str = ""
    """Unique response identifier from the API (empty if not provided)."""

    finish_reason: str = "stop"
    """Provider finish reason normalized by the response builder."""

    transport: TransportMetadata = Field(default_factory=TransportMetadata)
    """Typed transport metadata from the AIPL proxy and LiteLLM."""

    # Tool calling
    tool_calls: tuple[RawToolCall, ...] = ()
    """Tool calls requested by the model. Empty tuple when no tools were called."""

    # Provider-specific fields for multi-turn reasoning
    thinking_blocks: tuple[dict[str, Any], ...] | None = None
    """Structured thinking blocks from the model (if available)."""

    provider_specific_fields: Mapping[str, Any] | None = None
    """Carriers for provider-specific reasoning state across multi-turn conversations."""

    @property
    def has_tool_calls(self) -> bool:
        """Whether the model requested tool calls in this response."""
        return len(self.tool_calls) > 0

    @field_serializer("parsed", when_used="always")
    def serialize_parsed(self, value: T) -> Any:
        """Serialize parsed value - convert BaseModel or list[BaseModel] to dict/list."""
        if isinstance(value, BaseModel):
            return value.model_dump()
        if isinstance(value, list):
            if all(isinstance(item, BaseModel) for item in value):
                return [item.model_dump() for item in value]
            return value
        return value
