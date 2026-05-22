"""Large Language Model integration via LiteLLM proxy.

This package provides the Conversation class for LLM interactions with built-in
retry logic and structured output generation using Pydantic models.

Primary API:
    Conversation class - Immutable, Document-based LLM interaction

Primitive types are re-exported from _llm_core.
"""

from ai_pipeline_core._llm_core.model_response import Citation
from ai_pipeline_core._llm_core.types import AIModel, ImagePreset, ModelOptions, TokenUsage

from .conversation import Conversation
from .tools import Tool, ToolOutput

__all__ = [
    "AIModel",
    "Citation",
    "Conversation",
    "ImagePreset",
    "ModelOptions",
    "TokenUsage",
    "Tool",
    "ToolOutput",
]
