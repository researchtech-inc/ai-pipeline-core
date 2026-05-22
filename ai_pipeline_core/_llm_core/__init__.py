"""Streaming-first AIPL-aware internal LLM core.

The public application boundary is ``ai_pipeline_core.llm.Conversation``.
This package exposes typed content, request, response, and transport metadata
objects. The low-level ``client.generate`` function is intentionally not
re-exported here so application code cannot bypass the engine.
"""

from ._transport_metadata import AIPLInfo, LiteLLMInfo, TransportMetadata
from .model_response import Citation, ModelResponse
from .request import (
    CacheSpec,
    DebugSpec,
    GenerationSpec,
    ListOf,
    LLMRequest,
    ResponseSpec,
    RetrySpec,
    RoutingSpec,
    ToolSpec,
    ValidationSpec,
)
from .types import (
    AIModel,
    ContentPart,
    CoreMessage,
    ImageContent,
    ImagePreset,
    PDFContent,
    RawToolCall,
    Role,
    TextContent,
    TokenUsage,
)

__all__ = [
    "AIModel",
    "AIPLInfo",
    "CacheSpec",
    "Citation",
    "ContentPart",
    "CoreMessage",
    "DebugSpec",
    "GenerationSpec",
    "ImageContent",
    "ImagePreset",
    "LLMRequest",
    "ListOf",
    "LiteLLMInfo",
    "ModelResponse",
    "PDFContent",
    "RawToolCall",
    "ResponseSpec",
    "RetrySpec",
    "Role",
    "RoutingSpec",
    "TextContent",
    "TokenUsage",
    "ToolSpec",
    "TransportMetadata",
    "ValidationSpec",
]
