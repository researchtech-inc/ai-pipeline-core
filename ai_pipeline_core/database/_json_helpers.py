"""Shared JSON helpers for database- and replay-layer record payloads."""

import json
from typing import Any

__all__ = [
    "json_dumps",
    "parse_json_object",
]


def parse_json_object(payload_json: str, *, context: str, field_name: str = "JSON field") -> dict[str, Any]:
    """Parse a stored JSON object payload with consistent validation errors."""
    try:
        value = json.loads(payload_json or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{context} has invalid {field_name}. "
            f"Store a valid JSON object string in {field_name} before reading this record."
        ) from exc
    if not isinstance(value, dict):
        raise TypeError(
            f"{context} {field_name} must decode to a JSON object. "
            f"Store an object like {{}} instead of {type(value).__name__}."
        )
    return value


def json_dumps(value: Any) -> str:
    """Serialize JSON using the framework's canonical stable formatting."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
