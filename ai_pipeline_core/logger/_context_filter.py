"""Inject execution metadata into stdlib log records."""

import logging
from typing import Any, override

from ai_pipeline_core._execution_context_state import get_execution_context_state
from ai_pipeline_core.pipeline._execution_context import ExecutionContext

__all__ = ["ExecutionContextFilter"]


class ExecutionContextFilter(logging.Filter):
    """Attach execution metadata from the active ``ExecutionContext``."""

    @override
    def filter(self, record: logging.LogRecord) -> bool:
        """Populate record fields when an execution context is active."""
        ctx = get_execution_context_state()
        if not isinstance(ctx, ExecutionContext):
            return True

        flow_name = ctx.flow_frame.name if ctx.flow_frame is not None else ""
        task_name = ctx.task_frame.task_class_name if ctx.task_frame is not None else ""
        values: dict[str, Any] = {
            "run_id": ctx.run_id,
            "deployment_id": str(ctx.deployment_id) if ctx.deployment_id is not None else "",
            "root_deployment_id": str(ctx.root_deployment_id) if ctx.root_deployment_id is not None else "",
            "deployment_name": ctx.deployment_name,
            "span_id": str(ctx.current_span_id) if ctx.current_span_id is not None else "",
            "span_kind": ctx.span_kind,
            "span_name": ctx.span_name,
            "span_target": ctx.span_target,
            "flow_name": flow_name,
            "task_name": task_name,
            "service": ctx.service_name,
        }
        for field_name, value in values.items():
            if field_name not in record.__dict__:
                setattr(record, field_name, value)
        return True
