"""Verify Laminar initialization failures are logged at ERROR level.

Systemic failures (init failure, project key mismatch) disable all span export
for the process — they must be ERROR, not WARNING. Per-span transient failures
remain WARNING.
"""

# pyright: reportPrivateUsage=false

import inspect

from ai_pipeline_core.observability._laminar_sink import LaminarSpanSink


class TestLaminarSystemicFailuresUseErrorLevel:
    """Systemic failures that disable all Laminar export must log at ERROR."""

    def test_initialization_failure_uses_error_level(self) -> None:
        source = inspect.getsource(LaminarSpanSink._initialize_laminar)
        assert "logger.error" in source
        assert "logger.warning" not in source

    def test_project_switch_uses_error_level(self) -> None:
        source = inspect.getsource(LaminarSpanSink._warn_project_switch)
        assert "logger.error" in source
        assert "logger.warning" not in source

    def test_different_key_after_failure_uses_error_level(self) -> None:
        source = inspect.getsource(LaminarSpanSink._initialization_state_allows_export)
        assert "logger.error" in source


class TestLaminarTransientFailuresUseWarningLevel:
    """Per-span export failures remain at WARNING — they are transient."""

    def test_span_start_failure_uses_warning_level(self) -> None:
        source = inspect.getsource(LaminarSpanSink.on_span_started)
        assert "logger.warning" in source
        assert "logger.error" not in source

    def test_span_finish_failure_uses_warning_level(self) -> None:
        source = inspect.getsource(LaminarSpanSink.on_span_finished)
        assert "logger.warning" in source
        assert "logger.error" not in source
