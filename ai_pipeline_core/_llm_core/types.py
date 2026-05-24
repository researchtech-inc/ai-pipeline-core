"""Primitive types and constants for LLM interactions.

This module provides low-level types for LLM communication that have NO dependency
on the Document class. These types are used by internal modules (database,
observability) that need LLM access but cannot depend on Documents.

All types are frozen Pydantic models for immutability and JSON serialization.
"""

import json
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Literal, Self, cast

from pydantic import Base64Bytes, BaseModel, ConfigDict, Field, field_validator, model_validator

from ._defaults import DEFAULT_CACHE_TTL_MINUTES, cache_ttl_to_wire

TOKENS_PER_IMAGE = 1080
"""Coarse per-image token estimate used by ``_llm_core`` and Conversation."""


def estimate_image_tokens() -> int:
    """Estimate tokens for one image part. Local to ``_llm_core`` for standalone use."""
    return TOKENS_PER_IMAGE


type JsonValue = Any

__all__ = [
    "TOKENS_PER_IMAGE",
    "AIModel",
    "ContentPart",
    "CoreMessage",
    "ImageContent",
    "ImagePreset",
    "ModelOptions",
    "PDFContent",
    "RawToolCall",
    "Role",
    "TextContent",
    "TokenUsage",
    "estimate_image_tokens",
]


class ImagePreset(StrEnum):
    """Image processing preset carried explicitly by AIModel."""

    DEFAULT = "default"
    HIGH_RES = "high_res"
    BALANCED = "balanced"
    COMPACT = "compact"


class Role(StrEnum):
    """Message role in conversation."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class AIModel(BaseModel):
    """Complete model configuration used by the LLM execution path.

    Use ``AIModel("model-name")`` or ``AIModel(name="model-name")`` at the
    configuration/FlowOptions boundary, then pass the resolved instance through
    the rest of the framework. Capabilities (structured output, tools, images,
    PDFs, URL preservation) are declared explicitly on the AIModel; the
    framework never infers them from the model name.

    Capability flags fall into two semantic groups:

    * ``supports_structured_output``, ``supports_tools``, ``supports_images``,
      ``supports_pdfs`` — pre-flight gates. When a request asks for a feature
      the model declares as ``False``, the framework advances to the
      ``fallback`` chain (if any) without spending an attempt. When no
      fallback is set, the call fails fast with ``TerminalError``.
    * ``supports_json_schema`` — opt-in optimization. ``False`` (default,
      safe) appends a human-readable description of the response schema
      (field names, types, one-line example) to the last USER message so
      models that do not honor strict ``json_schema`` still produce
      conformant JSON. ``True`` skips injection on the assumption the
      provider/deployment honors the wire-level schema; if structured
      validation then fails repeatedly, the framework warns once and
      advances the fallback chain via ``StructuredRepairExhaustedError``.

    ``cache_ttl`` is integer minutes: ``0`` disables explicit cache markers,
    and ``1..1440`` map to the seconds wire format on the request.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(description="Model name as recognized by the LiteLLM proxy.")
    timeout_s: float = Field(default=600.0, gt=0, description="Per-hop wall-clock timeout in seconds.")
    temperature: float | None = None
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh"] = "medium"
    verbosity: Literal["low", "medium", "high"] | None = None
    max_completion_tokens: int | None = None
    supports_stop_sequences: bool = True
    supports_structured_output: bool = True
    supports_tools: bool = True
    supports_images: bool = True
    supports_pdfs: bool = True
    supports_json_schema: bool = False
    """When False (safe default), append a prose schema description to the
    last USER message for structured-output requests. Set True only for
    (model, provider) pairs that natively honor strict ``json_schema``."""
    vision_preset: ImagePreset = ImagePreset.DEFAULT
    preserve_input_urls: bool = False
    supports_url_substitution: bool = True
    cache_ttl: int = DEFAULT_CACHE_TTL_MINUTES
    skip_cost_optimized: bool = False
    max_inline_file_total_bytes: int = 50_000_000
    """Raw-byte cap on inline image+PDF payloads per request before base64 expansion."""
    fallback: AIModel | None = None

    def __init__(self, name: str | None = None, /, **data: Any) -> None:
        """Allow ``AIModel("model-name")`` while keeping keyword construction.

        ``name`` is positional-only here so callers cannot pass both
        ``AIModel("foo", name="bar")``. Pydantic's ``model_validator`` enforces
        that ``name`` is set (either positionally or via kwargs).
        """
        if name is not None:
            if "name" in data:
                raise TypeError("AIModel name was provided both positionally and as a keyword.")
            data["name"] = name
        super().__init__(**data)

    @field_validator("cache_ttl", mode="before")
    @classmethod
    def _validate_cache_ttl(cls, value: Any) -> int:
        cache_ttl_to_wire(value)  # raises TypeError/ValueError on invalid input
        return value

    @model_validator(mode="after")
    def _detect_fallback_cycle(self) -> Self:
        """Reject fallback cycles at construction time."""
        seen = {self.name}
        chain = [self.name]
        current = self.fallback
        while current is not None:
            chain.append(current.name)
            if current.name in seen:
                raise ValueError(f"AIModel fallback cycle detected: {' -> '.join(chain)}.")
            seen.add(current.name)
            current = current.fallback
        return self


class RawToolCall(BaseModel):
    """A single tool call from an LLM response.

    Represents the raw tool call data from the API. The arguments field is always
    a JSON string — some providers return a dict which is coerced to a JSON
    string automatically.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    function_name: str
    arguments: str
    provider_specific_fields: Mapping[str, JsonValue] | None = None

    @field_validator("arguments", mode="before")
    @classmethod
    def _coerce_arguments(cls, v: Any) -> str:
        if isinstance(v, dict):
            return json.dumps(v)
        return v


class TextContent(BaseModel):
    """Plain text content part."""

    model_config = ConfigDict(frozen=True)

    type: Literal["text"] = "text"
    text: str


class ImageContent(BaseModel):
    """Image content with binary data.

    Uses Base64Bytes for automatic base64 encoding/decoding in JSON serialization.
    """

    model_config = ConfigDict(frozen=True)

    type: Literal["image"] = "image"
    data: Base64Bytes
    mime_type: Literal["image/jpeg", "image/png", "image/gif", "image/webp"]


class PDFContent(BaseModel):
    """PDF document content with binary data.

    Uses Base64Bytes for automatic base64 encoding/decoding in JSON serialization.
    """

    model_config = ConfigDict(frozen=True)

    type: Literal["pdf"] = "pdf"
    data: Base64Bytes
    filename: str = "document.pdf"


ContentPart = TextContent | ImageContent | PDFContent
"""Union of all content part types for polymorphic handling."""


class CoreMessage(BaseModel):
    """A single message in a conversation.

    Content can be:
    - str: Plain text (converted to TextContent internally)
    - ContentPart: Single content part (text, image, or PDF)
    - tuple[ContentPart, ...]: Multiple content parts (multimodal message)

    Tool-related fields:
    - tool_calls: present on ASSISTANT messages that request tool execution
    - tool_call_id + name: present on TOOL messages carrying tool results

    Reasoning carriers (provider_specific_fields, thinking_blocks) round-trip
    provider-supplied state (e.g. Gemini ``thought_signature``,
    OpenAI Responses ``reasoning_items``, Anthropic ``thinking_blocks``)
    across tool-loop turns so the model can continue from prior reasoning.
    """

    model_config = ConfigDict(frozen=True)

    role: Role
    content: str | ContentPart | tuple[ContentPart, ...]
    tool_calls: tuple[RawToolCall, ...] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    provider_specific_fields: Mapping[str, JsonValue] | None = None
    thinking_blocks: tuple[Mapping[str, JsonValue], ...] | None = None

    @model_validator(mode="after")
    def _validate_tool_fields(self) -> Self:
        if self.tool_calls and self.role != Role.ASSISTANT:
            raise ValueError("tool_calls is only valid on ASSISTANT messages")
        if self.tool_call_id is not None and self.role != Role.TOOL:
            raise ValueError("tool_call_id is only valid on TOOL messages")
        if self.role == Role.TOOL and self.tool_call_id is None:
            raise ValueError("TOOL messages require tool_call_id")
        if self.role == Role.TOOL and not isinstance(self.content, str):
            raise ValueError("TOOL messages must have str content")
        return self


class TokenUsage(BaseModel):
    """Token usage statistics from an LLM call."""

    model_config = ConfigDict(frozen=True)

    prompt_tokens: int  # Input tokens consumed by prompt and context
    completion_tokens: int  # Output tokens generated by the model
    total_tokens: int  # prompt_tokens + completion_tokens
    cached_tokens: int = 0  # Prompt tokens served from provider cache
    cache_creation_tokens: int = 0  # Prompt tokens billed for creating an explicit provider cache
    reasoning_tokens: int = 0  # Tokens used for internal model reasoning


class ModelOptions(BaseModel):
    """Conversation-level overrides translated into request specs.

    ``cache_ttl=None`` (sentinel) inherits the AIModel's ``cache_ttl`` value.
    Set to an explicit integer (``0..1440``) to override.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    temperature: float | None = None  # Leave unset (provider decides) unless you have a specific reason
    system_prompt: str | None = None  # Prepended to messages; substitutor instructions appended when active
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh"] | None = None
    retries: int | None = Field(default=None, ge=0)  # None = use Settings.conversation_retries.
    retry_delay_seconds: float | None = Field(default=None, ge=0)  # None = use Settings exponential backoff.
    timeout: float | None = Field(default=None, gt=0)
    # Cache TTL in integer minutes. ``None`` inherits from AIModel; ``0`` disables
    # explicit cache markers; ``1..1440`` map to the seconds wire format on the request.
    cache_ttl: int | None = None
    max_completion_tokens: int | None = Field(default=None, gt=0)  # Defaults to provider behavior (~30K typical)
    stop: tuple[str, ...] | None = None
    verbosity: Literal["low", "medium", "high"] | None = None

    @field_validator("cache_ttl", mode="before")
    @classmethod
    def _validate_cache_ttl(cls, v: Any) -> int | None:
        if v is None:
            return None
        cache_ttl_to_wire(v)
        return v

    @field_validator("stop", mode="before")
    @classmethod
    def _coerce_stop(cls, v: Any) -> tuple[str, ...] | None:
        if v is None:
            return None
        if isinstance(v, str):
            return (v,)
        if isinstance(v, (list, tuple)):
            return tuple(str(s) for s in cast(list[Any], v))
        return v
