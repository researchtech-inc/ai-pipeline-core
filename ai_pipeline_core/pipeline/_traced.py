"""Lightweight traced-operation spans backed by append-only span rows."""

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from ai_pipeline_core.database import SpanKind
from ai_pipeline_core.pipeline._execution_context import (
    TaskFrame,
    get_execution_context,
    get_sinks,
    set_execution_context,
)
from ai_pipeline_core.pipeline._track_span import track_span

__all__ = ["traced_operation"]

_TRACED_OPERATION_CLASS_NAME = "traced_operation"


@asynccontextmanager
async def traced_operation(name: str, description: str = "") -> AsyncGenerator[None]:
    """Create a tracked operation span. Outside deployment context this is a no-op."""
    execution_ctx = get_execution_context()
    if execution_ctx is None or execution_ctx.database is None or execution_ctx.deployment_id is None:
        yield
        return

    parent_task = execution_ctx.task_frame
    with contextlib.ExitStack() as stack:
        async with track_span(
            SpanKind.OPERATION,
            name,
            "",
            sinks=get_sinks(),
            db=execution_ctx.database,
            input_preview=None,
        ) as span_ctx:
            task_frame = TaskFrame(
                task_class_name=_TRACED_OPERATION_CLASS_NAME,
                task_id=str(span_ctx.span_id),
                depth=(parent_task.depth + 1) if parent_task is not None else 0,
                parent=parent_task,
            )
            traced_ctx = get_execution_context()
            if traced_ctx is not None:
                stack.enter_context(set_execution_context(traced_ctx.with_task(task_frame)))
            span_ctx.set_meta(description=description)
            try:
                yield
            except (Exception, asyncio.CancelledError):  # fmt: skip
                span_ctx.set_meta(description=description)
                raise
