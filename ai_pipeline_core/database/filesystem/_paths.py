"""Filesystem path helpers for hierarchical snapshot layouts."""

import re
from datetime import datetime
from uuid import UUID

__all__ = ["UNNAMED_FALLBACK", "run_directory_name", "sanitize_name", "span_filename"]

MAX_SANITIZED_NAME_LENGTH = 64
UNNAMED_FALLBACK = "unnamed"
_UNSAFE_CHARS_RE = re.compile(r"[^a-zA-Z0-9_-]")
_MULTI_DASH_RE = re.compile(r"-{2,}")
_KIND_PREFIXES = {
    "flow": "flow",
    "task": "task",
    "conversation": "conv",
    "prompt_execution": "prompt",
    "operation": "operation",
    "deployment": "deployment",
}
_EMBEDS_LLM_ROUNDS = frozenset({"conversation", "prompt_execution"})


def sanitize_name(name: str) -> str:
    """Return a filesystem-safe name segment."""
    sanitized = _UNSAFE_CHARS_RE.sub("-", name)
    sanitized = _MULTI_DASH_RE.sub("-", sanitized).strip("-")
    if not sanitized:
        return UNNAMED_FALLBACK
    return sanitized[:MAX_SANITIZED_NAME_LENGTH]


def run_directory_name(started_at: datetime, name: str | None, span_id: UUID) -> str:
    """Return the run directory name for a deployment root span."""
    date_part = started_at.strftime("%Y-%m-%d")
    return f"{date_part}_{sanitize_name(name or UNNAMED_FALLBACK)}_{str(span_id).replace('-', '')[:8]}"


def span_filename(
    kind: str,
    name: str | None,
    span_id: UUID,
    sibling_index: int,
) -> str:
    """Return the local filename for a non-root span."""
    prefix = _KIND_PREFIXES.get(kind, sanitize_name(kind))
    index_prefix = f"{sibling_index:02d}_"
    if kind in _EMBEDS_LLM_ROUNDS:
        return f"{index_prefix}{prefix}-{str(span_id).replace('-', '')[:8]}.json"
    return f"{index_prefix}{prefix}-{sanitize_name(name or UNNAMED_FALLBACK)}.json"
