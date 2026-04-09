"""Tests for remote deployment utilities."""

import asyncio
from contextlib import nullcontext
from functools import wraps
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from httpx import HTTPStatusError, Request, RequestError, Response
from prefect.exceptions import ObjectNotFound


def _make_http_status_error(status_code: int, detail: str) -> HTTPStatusError:
    request = Request("POST", "https://prefect.example/api/deployments/test/create_flow_run")
    response = Response(status_code, request=request, text=detail)
    return HTTPStatusError(f"{status_code} error", request=request, response=response)


def _make_request_error(message: str) -> RequestError:
    request = Request("POST", "https://prefect.example/api/deployments/test/create_flow_run")
    return RequestError(message, request=request)


def _make_created_flow_run(flow_run_id: int = 1) -> MagicMock:
    flow_run = MagicMock()
    flow_run.id = UUID(int=flow_run_id)
    return flow_run


def _make_flow_run(
    *,
    is_final: bool = False,
    is_completed: bool = False,
    result: Any = None,
    error: Exception | None = None,
    state_name: str = "Completed",
    state_type: str = "COMPLETED",
) -> MagicMock:
    flow_run = MagicMock()
    flow_run.id = UUID(int=1)
    flow_run.state = MagicMock()
    flow_run.state.is_final.return_value = is_final
    flow_run.state.is_completed.return_value = is_completed
    flow_run.state.name = state_name
    flow_run.state.type = state_type
    flow_run.state.result = AsyncMock(side_effect=error) if error is not None else AsyncMock(return_value=result)
    return flow_run


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


class TestRunRemoteDeployment:
    """Test remote Prefect submission and fallback behavior."""

    async def test_not_found_without_prefect_api_url_raises_actionable_error(self) -> None:
        from ai_pipeline_core.deployment.remote import RemoteDeploymentNotFoundError, _run_remote_deployment

        with patch("ai_pipeline_core.deployment.remote.get_client") as mock_get_client:
            local_client = AsyncMock()
            local_client.__aenter__.return_value = local_client
            local_client.__aexit__.return_value = None
            mock_get_client.return_value = local_client

            with (
                patch(
                    "ai_pipeline_core.deployment.remote.run_deployment",
                    new=AsyncMock(side_effect=ObjectNotFound(http_exc=Exception("missing"))),
                ),
                patch("ai_pipeline_core.deployment.remote.settings") as mock_settings,
            ):
                mock_settings.prefect_api_url = None

                with pytest.raises(RemoteDeploymentNotFoundError, match="PREFECT_API_URL"):
                    await _run_remote_deployment("test-deployment", {"run_id": "abc-123"})

    async def test_falls_back_to_explicit_client_only_for_object_not_found(self) -> None:
        from ai_pipeline_core.deployment.remote import _run_remote_deployment

        local_run_error = ObjectNotFound(http_exc=Exception("missing locally"))
        remote_flow_run = _make_created_flow_run(flow_run_id=7)

        with patch("ai_pipeline_core.deployment.remote.get_client") as mock_get_client:
            local_client = AsyncMock()
            local_client.__aenter__.return_value = local_client
            local_client.__aexit__.return_value = None
            mock_get_client.return_value = local_client

            with (
                patch("ai_pipeline_core.deployment.remote.PrefectClient") as mock_prefect_client,
                patch("ai_pipeline_core.deployment.remote.AsyncClientContext.model_construct", return_value=nullcontext()),
                patch(
                    "ai_pipeline_core.deployment.remote.run_deployment",
                    new=AsyncMock(side_effect=[local_run_error, remote_flow_run]),
                ) as mock_run_deployment,
                patch("ai_pipeline_core.deployment.remote._poll_remote_flow_run", new=AsyncMock(return_value={"success": True})) as mock_poll,
                patch("ai_pipeline_core.deployment.remote.settings") as mock_settings,
            ):
                remote_client = AsyncMock()
                remote_client.__aenter__.return_value = remote_client
                remote_client.__aexit__.return_value = None
                mock_prefect_client.return_value = remote_client
                mock_settings.prefect_api_url = "https://prefect.example/api"
                mock_settings.prefect_api_key = "api-key"
                mock_settings.prefect_api_auth_string = "user:pass"

                result = await _run_remote_deployment("test-deployment", {"run_id": "abc-123"})

        assert result == {"success": True}
        assert mock_run_deployment.await_count == 2
        first_call = mock_run_deployment.await_args_list[0]
        second_call = mock_run_deployment.await_args_list[1]
        assert first_call.kwargs["as_subflow"] is True
        assert second_call.kwargs["as_subflow"] is False
        assert first_call.kwargs["idempotency_key"] == "abc-123"
        assert second_call.kwargs["idempotency_key"] == "abc-123"
        mock_poll.assert_awaited_once_with(remote_client, UUID(int=7))

    async def test_missing_everywhere_raises_not_found_error(self) -> None:
        from ai_pipeline_core.deployment.remote import RemoteDeploymentNotFoundError, _run_remote_deployment

        with patch("ai_pipeline_core.deployment.remote.get_client") as mock_get_client:
            local_client = AsyncMock()
            local_client.__aenter__.return_value = local_client
            local_client.__aexit__.return_value = None
            mock_get_client.return_value = local_client

            not_found_error = ObjectNotFound(http_exc=Exception("missing"))
            with (
                patch("ai_pipeline_core.deployment.remote.PrefectClient") as mock_prefect_client,
                patch("ai_pipeline_core.deployment.remote.AsyncClientContext.model_construct", return_value=nullcontext()),
                patch(
                    "ai_pipeline_core.deployment.remote.run_deployment",
                    new=AsyncMock(side_effect=[not_found_error, not_found_error]),
                ),
                patch("ai_pipeline_core.deployment.remote.settings") as mock_settings,
            ):
                remote_client = AsyncMock()
                remote_client.__aenter__.return_value = remote_client
                remote_client.__aexit__.return_value = None
                mock_prefect_client.return_value = remote_client
                mock_settings.prefect_api_url = "https://prefect.example/api"
                mock_settings.prefect_api_key = "api-key"
                mock_settings.prefect_api_auth_string = "user:pass"

                with pytest.raises(RemoteDeploymentNotFoundError, match="configured Prefect API"):
                    await _run_remote_deployment("test-deployment", {"run_id": "abc-123"})

    async def test_requires_non_empty_run_id_for_idempotency(self) -> None:
        from ai_pipeline_core.deployment.remote import _run_remote_deployment

        with pytest.raises(ValueError, match="include a non-empty 'run_id'"):
            await _run_remote_deployment("test-deployment", {"options": {}})


class TestSubmitRemoteFlowRun:
    """Test submission retry and error classification."""

    async def test_retries_retryable_http_status_and_reuses_run_id_as_idempotency_key(self) -> None:
        from ai_pipeline_core.deployment.remote import _submit_remote_flow_run

        client = AsyncMock()
        retryable_error = _make_http_status_error(500, "server busy")

        with (
            patch(
                "ai_pipeline_core.deployment.remote.run_deployment",
                new=AsyncMock(side_effect=[retryable_error, _make_created_flow_run()]),
            ) as mock_run_deployment,
            patch("ai_pipeline_core.deployment.remote.random.SystemRandom.uniform", return_value=0.0),
            patch("ai_pipeline_core.deployment.remote.asyncio.sleep", new=AsyncMock()) as mock_sleep,
        ):
            flow_run = await _submit_remote_flow_run(
                client=client,
                deployment_name="test-deployment",
                parameters={"run_id": "abc-123"},
                idempotency_key="abc-123",
                as_subflow=True,
            )

        assert flow_run.id == UUID(int=1)
        assert mock_run_deployment.await_count == 2
        for await_call in mock_run_deployment.await_args_list:
            assert await_call.kwargs["idempotency_key"] == "abc-123"
        mock_sleep.assert_awaited_once_with(1.0)

    async def test_retries_request_errors_and_then_raises_with_original_cause(self) -> None:
        from ai_pipeline_core.deployment.remote import RemoteDeploymentSubmissionError, _submit_remote_flow_run

        client = AsyncMock()
        transport_error = _make_request_error("connection dropped")

        with (
            patch(
                "ai_pipeline_core.deployment.remote.run_deployment",
                new=AsyncMock(side_effect=[transport_error, transport_error, transport_error]),
            ),
            patch("ai_pipeline_core.deployment.remote.random.SystemRandom.uniform", return_value=0.0),
            patch("ai_pipeline_core.deployment.remote.asyncio.sleep", new=AsyncMock()) as mock_sleep,
        ):
            with pytest.raises(RemoteDeploymentSubmissionError, match="idempotency key 'abc-123'") as excinfo:
                await _submit_remote_flow_run(
                    client=client,
                    deployment_name="test-deployment",
                    parameters={"run_id": "abc-123"},
                    idempotency_key="abc-123",
                    as_subflow=True,
                )

        assert excinfo.value.__cause__ is transport_error
        assert mock_sleep.await_count == 2
        assert mock_sleep.await_args_list[0].args == (1.0,)
        assert mock_sleep.await_args_list[1].args == (2.0,)

    async def test_non_retryable_http_error_raises_immediately_with_detail(self) -> None:
        from ai_pipeline_core.deployment.remote import RemoteDeploymentSubmissionError, _submit_remote_flow_run

        client = AsyncMock()
        validation_error = _make_http_status_error(422, "field 'options' is invalid")

        with patch(
            "ai_pipeline_core.deployment.remote.run_deployment",
            new=AsyncMock(side_effect=validation_error),
        ) as mock_run_deployment:
            with pytest.raises(RemoteDeploymentSubmissionError, match="field 'options' is invalid") as excinfo:
                await _submit_remote_flow_run(
                    client=client,
                    deployment_name="test-deployment",
                    parameters={"run_id": "abc-123"},
                    idempotency_key="abc-123",
                    as_subflow=True,
                )

        assert excinfo.value.__cause__ is validation_error
        mock_run_deployment.assert_awaited_once()

    async def test_retries_http_429_with_jittered_backoff(self) -> None:
        from ai_pipeline_core.deployment.remote import _submit_remote_flow_run

        client = AsyncMock()
        rate_limit_error = _make_http_status_error(429, "too many requests")

        with (
            patch(
                "ai_pipeline_core.deployment.remote.run_deployment",
                new=AsyncMock(side_effect=[rate_limit_error, _make_created_flow_run(flow_run_id=2)]),
            ) as mock_run_deployment,
            patch("ai_pipeline_core.deployment.remote.random.SystemRandom.uniform", return_value=0.125),
            patch("ai_pipeline_core.deployment.remote.asyncio.sleep", new=AsyncMock()) as mock_sleep,
        ):
            flow_run = await _submit_remote_flow_run(
                client=client,
                deployment_name="test-deployment",
                parameters={"run_id": "abc-123"},
                idempotency_key="abc-123",
                as_subflow=True,
            )

        assert flow_run.id == UUID(int=2)
        assert mock_run_deployment.await_count == 2
        mock_sleep.assert_awaited_once_with(1.125)

    async def test_cancelled_submission_is_never_retried(self) -> None:
        from ai_pipeline_core.deployment.remote import _submit_remote_flow_run

        client = AsyncMock()

        with (
            patch(
                "ai_pipeline_core.deployment.remote.run_deployment",
                new=AsyncMock(side_effect=asyncio.CancelledError()),
            ) as mock_run_deployment,
            patch("ai_pipeline_core.deployment.remote.asyncio.sleep", new=AsyncMock()) as mock_sleep,
        ):
            with pytest.raises(asyncio.CancelledError):
                await _submit_remote_flow_run(
                    client=client,
                    deployment_name="test-deployment",
                    parameters={"run_id": "abc-123"},
                    idempotency_key="abc-123",
                    as_subflow=True,
                )

        mock_run_deployment.assert_awaited_once()
        mock_sleep.assert_not_awaited()

    async def test_object_not_found_bubbles_for_client_fallback(self) -> None:
        from ai_pipeline_core.deployment.remote import _submit_remote_flow_run

        client = AsyncMock()
        not_found_error = ObjectNotFound(http_exc=Exception("missing"))

        with patch(
            "ai_pipeline_core.deployment.remote.run_deployment",
            new=AsyncMock(side_effect=not_found_error),
        ):
            with pytest.raises(ObjectNotFound):
                await _submit_remote_flow_run(
                    client=client,
                    deployment_name="test-deployment",
                    parameters={"run_id": "abc-123"},
                    idempotency_key="abc-123",
                    as_subflow=True,
                )


class TestPollRemoteFlowRun:
    """Test polling behavior and terminal-state handling."""

    async def test_returns_result_on_completion(self) -> None:
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
        assert client.read_flow_run.await_count == 2

    async def test_polling_preserves_original_error_chain(self) -> None:
        from ai_pipeline_core.deployment.remote import RemoteDeploymentPollingError, _poll_remote_flow_run

        client = AsyncMock()
        first_error = _make_request_error("poll failure 1")
        repeated_errors = [_make_request_error(f"poll failure {index}") for index in range(2, 11)]
        client.read_flow_run = AsyncMock(side_effect=[first_error, *repeated_errors])

        with pytest.raises(RemoteDeploymentPollingError, match="First error: RequestError") as excinfo:
            await _poll_remote_flow_run(client, UUID(int=1), poll_interval=0)

        assert excinfo.value.__cause__ is repeated_errors[-1]
        assert "Last error: RequestError: poll failure 10" in str(excinfo.value)

    async def test_failed_terminal_state_raises_framework_execution_error(self) -> None:
        from ai_pipeline_core.deployment.remote import RemoteDeploymentExecutionError, _poll_remote_flow_run

        client = AsyncMock()
        crashed_error = RuntimeError("worker crashed")
        client.read_flow_run = AsyncMock(
            side_effect=[
                _make_flow_run(
                    is_final=True,
                    is_completed=False,
                    error=crashed_error,
                    state_name="Crashed",
                    state_type="CRASHED",
                )
            ]
        )

        with pytest.raises(RemoteDeploymentExecutionError, match="terminal state 'Crashed'") as excinfo:
            await _poll_remote_flow_run(client, UUID(int=1), poll_interval=0)

        assert excinfo.value.__cause__ is crashed_error

    async def test_completed_state_wraps_result_retrieval_error(self) -> None:
        from ai_pipeline_core.deployment.remote import RemoteDeploymentExecutionError, _poll_remote_flow_run

        client = AsyncMock()
        retrieval_error = ValueError("result store unavailable")
        client.read_flow_run = AsyncMock(
            side_effect=[
                _make_flow_run(
                    is_final=True,
                    is_completed=True,
                    error=retrieval_error,
                    state_name="Completed",
                    state_type="COMPLETED",
                )
            ]
        )

        with pytest.raises(RemoteDeploymentExecutionError, match="result retrieval failed") as excinfo:
            await _poll_remote_flow_run(client, UUID(int=1), poll_interval=0)

        assert excinfo.value.__cause__ is retrieval_error

    async def test_request_error_on_poll_retries_and_recovers(self) -> None:
        from ai_pipeline_core.deployment.remote import _poll_remote_flow_run

        client = AsyncMock()
        client.read_flow_run = AsyncMock(
            side_effect=[
                _make_request_error("API unavailable"),
                _make_flow_run(is_final=True, is_completed=True, result="recovered"),
            ]
        )

        result = await _poll_remote_flow_run(client, UUID(int=1), poll_interval=0)
        assert result == "recovered"
        assert client.read_flow_run.await_count == 2

    async def test_non_retryable_poll_error_fails_fast(self) -> None:
        from ai_pipeline_core.deployment.remote import RemoteDeploymentPollingError, _poll_remote_flow_run

        client = AsyncMock()
        forbidden_error = _make_http_status_error(403, "forbidden")
        client.read_flow_run = AsyncMock(side_effect=forbidden_error)

        with patch("ai_pipeline_core.deployment.remote.asyncio.sleep", new=AsyncMock()) as mock_sleep:
            with pytest.raises(RemoteDeploymentPollingError, match="non-retryable HTTP 403") as excinfo:
                await _poll_remote_flow_run(client, UUID(int=1), poll_interval=0)

        assert excinfo.value.__cause__ is forbidden_error
        client.read_flow_run.assert_awaited_once()
        mock_sleep.assert_not_awaited()

    async def test_poll_timeout_raises_framework_polling_error(self) -> None:
        from ai_pipeline_core.deployment.remote import RemoteDeploymentPollingError, _poll_remote_flow_run

        client = AsyncMock()
        client.read_flow_run = AsyncMock(side_effect=[_make_flow_run(), _make_flow_run(), _make_flow_run()])

        with pytest.raises(RemoteDeploymentPollingError, match=r"exceeded 0\.0s"):
            await _poll_remote_flow_run(
                client,
                UUID(int=1),
                poll_interval=0.01,
                max_poll_seconds=0.02,
            )
