"""Regression tests for verified execution-context bugs."""

from uuid import uuid4

from ai_pipeline_core.deployment._helpers import validate_run_id
from ai_pipeline_core.deployment._types import _NoopPublisher
from ai_pipeline_core.pipeline._execution_context import ReplayExecutionContext


def test_replay_execution_context_run_id_passes_validator() -> None:
    context = ReplayExecutionContext.create(
        source_span_id=uuid4(),
        database=None,
        publisher=_NoopPublisher(),
        sinks=(),
    )

    validate_run_id(context.run_id)
