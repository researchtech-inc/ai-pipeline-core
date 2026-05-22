"""Tests for the settings singleton."""

from types import MappingProxyType

from ai_pipeline_core.deployment._types import _NoopPublisher
from ai_pipeline_core.pipeline._execution_context import ExecutionContext, set_execution_context
from ai_pipeline_core.pipeline.limits import _SharedStatus
from ai_pipeline_core.settings import Settings, settings


def _build_context() -> ExecutionContext:
    return ExecutionContext(
        run_id="test-run",
        execution_id=None,
        publisher=_NoopPublisher(),
        limits=MappingProxyType({}),
        limits_status=_SharedStatus(),
    )


def test_settings_singleton_is_settings_instance() -> None:
    assert isinstance(settings, Settings)


def test_execution_context_does_not_replace_settings_singleton() -> None:
    with set_execution_context(_build_context()):
        assert isinstance(settings, Settings)
