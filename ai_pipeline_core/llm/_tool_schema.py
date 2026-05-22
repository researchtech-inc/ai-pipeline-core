"""Tool schema generation for LLM function calling."""

from textwrap import dedent
from typing import Any

from ai_pipeline_core._llm_core._strict_schema import ensure_strict_schema

from .tools import Tool


def generate_tool_schema(tool: Tool) -> dict[str, Any]:
    """Generate an OpenAI-compatible strict function schema from a Tool instance."""
    tool_cls = type(tool)
    schema = tool_cls.Input.model_json_schema()
    ensure_strict_schema(schema, context=f"Tool '{tool_cls.name}'.Input")
    return {
        "type": "function",
        "function": {
            "name": tool_cls.name,
            "description": dedent(tool_cls.__doc__ or "").strip(),
            "parameters": schema,
            "strict": True,
        },
    }
