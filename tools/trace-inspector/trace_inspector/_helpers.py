"""Shared helper functions for trace-inspector.

Consolidates JSON parsing, metric extraction, duration calculation, and
document-hash detection that were previously duplicated across modules.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Any

from ai_pipeline_core.database import SpanRecord
from ai_pipeline_core.database._types import get_token_count

__all__ = [
    "duration_seconds",
    "ensure_directory",
    "get_token_count",
    "parse_json_object",
    "parse_json_object_lenient",
    "prepare_output_dir",
    "safe_json",
    "write_text",
]

logger = logging.getLogger(__name__)


def parse_json_object(payload_json: str, *, context: str) -> dict[str, Any]:
    """Parse a stored JSON object payload with validation.

    Raises ValueError on invalid JSON, TypeError on non-object.
    """
    try:
        value = json.loads(payload_json or "{}")
    except json.JSONDecodeError as exc:
        msg = f"{context} contains invalid JSON. Persist a valid JSON object string before rendering the trace bundle."
        raise ValueError(msg) from exc
    if not isinstance(value, dict):
        msg = f"{context} must decode to a JSON object. Persist an object instead of {type(value).__name__}."
        raise TypeError(msg)
    return value


def parse_json_object_lenient(payload_json: str, *, context: str) -> dict[str, Any]:
    """Parse a stored JSON object payload, returning empty dict on invalid input.

    Used during trace loading where corrupted metrics should not abort the entire load.
    """
    try:
        value = json.loads(payload_json or "{}")
    except json.JSONDecodeError:
        # Swallowing is safe: corrupted metrics in a single span should not abort loading
        # the entire deployment tree. The span will render with zero metrics instead.
        logger.warning("%s contains invalid JSON in metrics/meta payload — defaulting to empty metrics.", context)
        return {}
    if isinstance(value, dict):
        return value
    return {}


def safe_json(payload_json: str) -> Any:
    """Parse JSON with a fallback to a raw wrapper on decode failure."""
    try:
        return json.loads(payload_json or "{}")
    except json.JSONDecodeError:
        return {"raw": payload_json}


def duration_seconds(span: SpanRecord) -> float | None:
    """Return the wall-clock duration of a span in seconds, or None if still running."""
    if span.ended_at is None:
        return None
    return (span.ended_at - span.started_at).total_seconds()


def root_task_ids_for_flow(
    tasks_by_flow: dict[Any, tuple[Any, ...]],
    tasks: dict[Any, Any],
    flow_id: Any,
    selected_task_ids: set[Any],
) -> tuple[Any, ...]:
    """Return root task IDs for a flow — tasks whose parent is not in the selection."""
    result: list[Any] = []
    for task_id in tasks_by_flow.get(flow_id, ()):
        if task_id not in selected_task_ids:
            continue
        parent_task_id = tasks[task_id].parent_task_id
        if parent_task_id is None or parent_task_id not in selected_task_ids:
            result.append(task_id)
    return tuple(result)


async def prepare_output_dir(output_dir: Path) -> None:
    """Reset and create the output directory."""
    await asyncio.to_thread(_reset_output_dir, output_dir)


def _reset_output_dir(output_dir: Path) -> None:
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)


async def write_text(path: Path, content: str) -> None:
    """Write text content to a file asynchronously."""
    await asyncio.to_thread(path.write_text, content, "utf-8")


async def ensure_directory(path: Path) -> None:
    """Create directory and parents if they don't exist."""
    await asyncio.to_thread(path.mkdir, parents=True, exist_ok=True)
