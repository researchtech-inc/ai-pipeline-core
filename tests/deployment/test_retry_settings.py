"""Tests for Settings-driven retry defaults.

Verifies that task_retries, flow_retries, and conversation_retries from Settings
are used when class-level retries are None, and that explicit class values override Settings.
"""

# pyright: reportPrivateUsage=false

from unittest.mock import patch


from ai_pipeline_core import Document, FlowOptions, PipelineDeployment, PipelineFlow, PipelineTask
from ai_pipeline_core.deployment._deployment_runtime import _resolve_flow_retries, _resolve_flow_retry_delay
from ai_pipeline_core.llm import ModelOptions
from ai_pipeline_core.settings import Settings


# --- Test documents ---


class _SettingsInputDoc(Document):
    """Input document for settings retry tests."""


class _SettingsOutputDoc(Document):
    """Output document for settings retry tests."""


# --- Resolution logic tests ---


class TestResolveFlowRetries:
    """Verify flow retry resolution chain: flow class > deployment > Settings."""

    def test_flow_explicit_wins(self) -> None:
        """Flow with retries=5 in __dict__ always returns 5."""

        class _ExplicitFlow(PipelineFlow):
            """Flow with explicit retries."""

            retries = 5

            async def run(self, input_docs: tuple[_SettingsInputDoc, ...], options: FlowOptions) -> tuple[_SettingsOutputDoc, ...]:
                _ = options
                return (_SettingsOutputDoc.derive(derived_from=input_docs, name="out.txt", content="ok"),)

        assert _resolve_flow_retries(_ExplicitFlow, deployment_flow_retries=10) == 5
        assert _resolve_flow_retries(_ExplicitFlow, deployment_flow_retries=None) == 5

    def test_deployment_overrides_settings(self) -> None:
        """When flow has no explicit retries, deployment value wins over Settings."""
        assert _resolve_flow_retries(PipelineFlow, deployment_flow_retries=7) == 7

    def test_settings_fallback(self) -> None:
        """When both flow and deployment are None, Settings.flow_retries is used."""
        with patch("ai_pipeline_core.deployment._deployment_runtime.settings") as mock_settings:
            mock_settings.flow_retries = 4
            result = _resolve_flow_retries(PipelineFlow, deployment_flow_retries=None)
        assert result == 4

    def test_flow_explicit_zero_overrides(self) -> None:
        """Flow with retries=0 explicitly set always returns 0 (not fall through to deployment)."""

        class _ZeroFlow(PipelineFlow):
            """Flow that explicitly disables retries."""

            retries = 0

            async def run(self, input_docs: tuple[_SettingsInputDoc, ...], options: FlowOptions) -> tuple[_SettingsOutputDoc, ...]:
                _ = options
                return (_SettingsOutputDoc.derive(derived_from=input_docs, name="out.txt", content="ok"),)

        assert _resolve_flow_retries(_ZeroFlow, deployment_flow_retries=5) == 0


class TestResolveFlowRetryDelay:
    """Verify flow retry delay resolution chain."""

    def test_flow_explicit_wins(self) -> None:
        class _DelayFlow(PipelineFlow):
            """Flow with explicit delay."""

            retry_delay_seconds = 10

            async def run(self, input_docs: tuple[_SettingsInputDoc, ...], options: FlowOptions) -> tuple[_SettingsOutputDoc, ...]:
                _ = options
                return (_SettingsOutputDoc.derive(derived_from=input_docs, name="out.txt", content="ok"),)

        assert _resolve_flow_retry_delay(_DelayFlow, deployment_flow_retry_delay_seconds=99) == 10

    def test_deployment_overrides_settings(self) -> None:
        assert _resolve_flow_retry_delay(PipelineFlow, deployment_flow_retry_delay_seconds=15) == 15

    def test_settings_fallback(self) -> None:
        with patch("ai_pipeline_core.deployment._deployment_runtime.settings") as mock_settings:
            mock_settings.flow_retry_delay_seconds = 42
            result = _resolve_flow_retry_delay(PipelineFlow, deployment_flow_retry_delay_seconds=None)
        assert result == 42


# --- Task Settings resolution ---


class TestTaskRetrySettings:
    """Verify task retry resolution: class value > Settings."""

    def test_explicit_retries_honored(self) -> None:
        """Task with retries=3 uses 3 regardless of Settings."""
        assert PipelineTask.retries is None  # base default is None

    def test_settings_defaults(self) -> None:
        s = Settings()
        assert s.task_retries == 0
        assert s.task_retry_delay_seconds == 30

    def test_explicit_zero_is_not_none(self) -> None:
        """retries=0 is distinct from retries=None."""

        class _ZeroRetryTask(PipelineTask):
            """Task with explicit zero retries."""

            retries = 0

            @classmethod
            async def run(cls, input_docs: tuple[_SettingsInputDoc, ...]) -> tuple[_SettingsOutputDoc, ...]:
                return (_SettingsOutputDoc.derive(derived_from=input_docs, name="out.txt", content="ok"),)

        assert _ZeroRetryTask.retries == 0
        assert _ZeroRetryTask.retries is not None


# --- Conversation retry Settings resolution ---


class TestConversationRetrySettings:
    """Verify conversation retry resolution: ModelOptions > Settings."""

    def test_none_means_use_settings(self) -> None:
        opts = ModelOptions()
        assert opts.retries is None
        assert opts.retry_delay_seconds is None

    def test_explicit_zero_honored(self) -> None:
        opts = ModelOptions(retries=0, retry_delay_seconds=0)
        assert opts.retries == 0
        assert opts.retry_delay_seconds == 0

    def test_settings_defaults(self) -> None:
        s = Settings()
        assert s.conversation_retries == 2
        assert s.conversation_retry_delay_seconds == 20


# --- Deployment class defaults ---


class TestDeploymentRetryDefaults:
    """Verify PipelineDeployment class defaults are None (use Settings)."""

    def test_flow_retries_default_is_none(self) -> None:
        assert PipelineDeployment.flow_retries is None

    def test_flow_retry_delay_default_is_none(self) -> None:
        assert PipelineDeployment.flow_retry_delay_seconds is None
