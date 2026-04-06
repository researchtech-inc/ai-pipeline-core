"""Tests for remote deployment utilities."""

from functools import wraps
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from ai_pipeline_core import (
    DeploymentResult,
    Document,
    FlowOptions,
    PipelineDeployment,
)
from ai_pipeline_core.pipeline import PipelineFlow

# --- Module-level test infrastructure ---


class SampleInputDoc(Document):
    """Input document for testing."""


class SampleOutputDoc(Document):
    """Output document for testing."""


class SampleFlow(PipelineFlow):
    """Sample flow for testing."""

    async def run(self, input_docs: tuple[SampleInputDoc, ...], options: FlowOptions) -> tuple[SampleOutputDoc, ...]:
        _ = (input_docs, options)
        return ()


class SampleResult(DeploymentResult):
    """Result type for testing."""

    report: str = ""


class SamplePipeline(PipelineDeployment[FlowOptions, SampleResult]):
    """Pipeline for testing."""

    flow_retries = 0

    def build_flows(self, options: FlowOptions) -> list[PipelineFlow]:
        return [SampleFlow()]

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> SampleResult:
        return SampleResult(success=True, report="done")


# --- _run_remote_deployment tests ---


class TestRunRemoteDeployment:
    """Test _run_remote_deployment function error handling."""

    async def test_not_found_no_api_url(self):
        """Test error when deployment not found and PREFECT_API_URL is not set."""
        from prefect.exceptions import ObjectNotFound

        from ai_pipeline_core.deployment.remote import _run_remote_deployment

        with patch("ai_pipeline_core.deployment.remote.get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.read_deployment_by_name = AsyncMock(side_effect=ObjectNotFound(http_exc=Exception("Not found")))
            mock_get_client.return_value = mock_client

            with patch("ai_pipeline_core.deployment.remote.settings") as mock_settings:
                mock_settings.prefect_api_url = None

                with pytest.raises(ValueError, match="not set"):
                    await _run_remote_deployment("test-deployment", {"param": "value"})

    async def test_not_found_anywhere(self):
        """Test error when deployment not found on local or remote Prefect."""
        from prefect.exceptions import ObjectNotFound

        from ai_pipeline_core.deployment.remote import _run_remote_deployment

        with patch("ai_pipeline_core.deployment.remote.get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.read_deployment_by_name = AsyncMock(side_effect=ObjectNotFound(http_exc=Exception("Not found")))
            mock_get_client.return_value = mock_client

            with patch("ai_pipeline_core.deployment.remote.PrefectClient") as mock_pc:
                mock_remote = AsyncMock()
                mock_remote.__aenter__.return_value = mock_remote
                mock_remote.__aexit__.return_value = None
                mock_remote.read_deployment_by_name = AsyncMock(side_effect=ObjectNotFound(http_exc=Exception("Not found remotely")))
                mock_pc.return_value = mock_remote

                with patch("ai_pipeline_core.deployment.remote.settings") as mock_settings:
                    mock_settings.prefect_api_url = "http://api.example.com"
                    mock_settings.prefect_api_key = "key"
                    mock_settings.prefect_api_auth_string = "auth"

                    with pytest.raises(ValueError, match="not found"):
                        await _run_remote_deployment("test-deployment", {"param": "value"})


# --- Utility function tests ---


class TestIsAlreadyTraced:
    """Test is_already_traced utility function."""

    def test_false_for_untraced(self):
        """Test returns False for untraced function."""
        from ai_pipeline_core.pipeline._type_validation import is_already_traced

        async def my_func() -> None:
            pass

        assert is_already_traced(my_func) is False

    def test_true_for_traced(self):
        """Test returns True for function with __is_traced__ attribute."""
        from ai_pipeline_core.pipeline._type_validation import is_already_traced

        async def my_func() -> None:
            pass

        my_func.__is_traced__ = True  # type: ignore[attr-defined]

        assert is_already_traced(my_func) is True

    def test_deep_wrapped_chain(self):
        """Test detects trace through __wrapped__ chain."""
        from ai_pipeline_core.pipeline._type_validation import is_already_traced

        async def base_func() -> None:
            pass

        base_func.__is_traced__ = True  # type: ignore[attr-defined]

        @wraps(base_func)
        async def wrapper() -> None:
            pass

        wrapper.__wrapped__ = base_func  # type: ignore[attr-defined]

        assert is_already_traced(wrapper) is True


# --- Polling tests ---


def _make_flow_run(
    labels: dict[str, Any] | None = None,
    *,
    is_final: bool = False,
    is_completed: bool = False,
    result: Any = None,
    error: Exception | None = None,
) -> MagicMock:
    """Create a mock FlowRun with controlled state and labels."""
    fr = MagicMock()
    fr.id = UUID(int=1)
    fr.labels = labels or {}
    fr.state = MagicMock()
    fr.state.is_final.return_value = is_final
    fr.state.is_completed.return_value = is_completed
    fr.state.result = AsyncMock(side_effect=error) if error else AsyncMock(return_value=result)
    return fr


class TestPollRemoteFlowRun:
    """Test _poll_remote_flow_run polls until final state."""

    async def test_returns_result_on_completion(self):
        """Poll returns result when flow run completes."""
        from ai_pipeline_core.deployment.remote import _poll_remote_flow_run

        client = AsyncMock()
        client.read_flow_run = AsyncMock(
            side_effect=[
                _make_flow_run(),
                _make_flow_run(is_final=True, is_completed=True, result={"success": True}),
            ]
        )

        result = await _poll_remote_flow_run(client, UUID(int=1), poll_interval=0)
        assert result == {"success": True}
        assert client.read_flow_run.call_count == 2

    async def test_raises_on_failure(self):
        """Poll raises when flow run fails."""
        from ai_pipeline_core.deployment.remote import _poll_remote_flow_run

        client = AsyncMock()
        client.read_flow_run = AsyncMock(
            side_effect=[
                _make_flow_run(is_final=True, is_completed=False, error=RuntimeError("Crashed")),
            ]
        )

        with pytest.raises(RuntimeError, match="Crashed"):
            await _poll_remote_flow_run(client, UUID(int=1), poll_interval=0)

    async def test_api_error_retries(self):
        """Prefect API error on poll → logged, continues polling, returns result."""
        from ai_pipeline_core.deployment.remote import _poll_remote_flow_run

        client = AsyncMock()
        client.read_flow_run = AsyncMock(
            side_effect=[
                ConnectionError("API unavailable"),
                _make_flow_run(is_final=True, is_completed=True, result="recovered"),
            ]
        )

        result = await _poll_remote_flow_run(client, UUID(int=1), poll_interval=0)
        assert result == "recovered"
        assert client.read_flow_run.call_count == 2
