"""Pure invariant helpers used by capability probes."""

from typing import Any

from pydantic import BaseModel

from ai_pipeline_core._llm_core.model_response import ModelResponse


def has_non_empty_content(response: ModelResponse[Any]) -> bool:
    """True when the response carries any non-whitespace text content."""
    return bool(response.content and response.content.strip())


def parsed_matches_model(response: ModelResponse[Any], model_cls: type[BaseModel]) -> bool:
    """True when the parsed payload is an instance of the expected model."""
    return isinstance(response.parsed, model_cls)


def has_tool_call(response: ModelResponse[Any], function_name: str) -> bool:
    """True when the model emitted at least one tool call targeting ``function_name``."""
    return any(call.function_name == function_name for call in response.tool_calls)


def marker_in_content(response: ModelResponse[Any], marker: str) -> bool:
    """Case-insensitive marker substring check on ``response.content``."""
    return marker.upper() in (response.content or "").upper()
