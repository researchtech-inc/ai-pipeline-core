"""Tests for PubSubPublisher, _classify_error, and CloudEvents envelope."""

# pyright: reportPrivateUsage=false

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from ai_pipeline_core.deployment._pubsub import (
    CLOUDEVENTS_SPEC_VERSION,
    MAX_PUBSUB_MESSAGE_BYTES,
    MAX_RETRIES,
    PubSubPublisher,
    ResultTooLargeError,
)
from ai_pipeline_core.deployment._types import (
    ErrorCode,
    EventType,
    RunCompletedEvent,
    RunFailedEvent,
    RunStartedEvent,
)
from ai_pipeline_core.deployment.base import _classify_error
from ai_pipeline_core.exceptions import LLMError, PipelineCoreError


class TestClassifyError:
    """Test _classify_error maps exception types to ErrorCode."""

    def test_llm_error(self):
        """LLMError maps to PROVIDER_ERROR."""
        assert _classify_error(LLMError("fail")) == ErrorCode.PROVIDER_ERROR

    def test_cancelled_error(self):
        """CancelledError maps to CANCELLED."""
        assert _classify_error(asyncio.CancelledError()) == ErrorCode.CANCELLED

    def test_timeout_error(self):
        """TimeoutError maps to DURATION_EXCEEDED."""
        assert _classify_error(TimeoutError("timed out")) == ErrorCode.DURATION_EXCEEDED

    def test_value_error(self):
        """ValueError maps to INVALID_INPUT."""
        assert _classify_error(ValueError("bad value")) == ErrorCode.INVALID_INPUT

    def test_type_error(self):
        """TypeError maps to INVALID_INPUT."""
        assert _classify_error(TypeError("bad type")) == ErrorCode.INVALID_INPUT

    def test_pipeline_core_error(self):
        """PipelineCoreError maps to PIPELINE_ERROR."""
        assert _classify_error(PipelineCoreError("pipeline fail")) == ErrorCode.PIPELINE_ERROR

    def test_unknown_error(self):
        """Unrecognized exceptions map to UNKNOWN."""
        assert _classify_error(RuntimeError("unknown")) == ErrorCode.UNKNOWN
        assert _classify_error(OSError("os error")) == ErrorCode.UNKNOWN

    def test_accepts_base_exception(self):
        """_classify_error accepts BaseException (not just Exception)."""
        assert _classify_error(KeyboardInterrupt()) == ErrorCode.UNKNOWN

    def test_llm_subclass_maps_to_provider(self):
        """Subclasses of LLMError also map to PROVIDER_ERROR."""

        class CustomLLMError(LLMError):
            """Custom LLM error."""

        assert _classify_error(CustomLLMError("fail")) == ErrorCode.PROVIDER_ERROR


def _make_pubsub_publisher() -> tuple[PubSubPublisher, MagicMock]:
    """Create a PubSubPublisher with mocked pubsub_v1."""
    mock_client = MagicMock()
    mock_client.topic_path.return_value = "projects/test/topics/events"

    pub = PubSubPublisher.__new__(PubSubPublisher)
    pub._client = mock_client
    pub._topic_path = "projects/test/topics/events"
    pub._service_type = "research"
    pub._seq = 0

    return pub, mock_client


class TestPubSubPublisher:
    """Test PubSubPublisher CloudEvents envelope and publish behavior."""

    def test_build_envelope_structure(self):
        """_build_envelope produces a valid CloudEvents 1.0 envelope."""
        pub, _ = _make_pubsub_publisher()
        data_bytes = pub._build_envelope(EventType.RUN_STARTED, "run-1", {"span_id": "span-1"})
        envelope = json.loads(data_bytes)

        assert envelope["specversion"] == CLOUDEVENTS_SPEC_VERSION
        assert envelope["type"] == "run.started"
        assert envelope["source"] == "ai-research-worker"
        assert envelope["subject"] == "run-1"
        assert envelope["datacontenttype"] == "application/json"
        assert UUID(envelope["id"]).version == 7
        assert "time" in envelope

        data = envelope["data"]
        assert data["run_id"] == "run-1"
        assert data["span_id"] == "span-1"

    async def test_publish_started(self):
        """publish_run_started publishes run.started event."""
        pub, mock_client = _make_pubsub_publisher()
        event = RunStartedEvent(
            run_id="run-1",
            span_id="span-1",
            root_deployment_id="root-1",
            parent_deployment_task_id=None,
            input_fingerprint="abc123456789abcd",
            status="running",
        )

        mock_future = asyncio.Future()
        mock_future.set_result("msg-id")
        mock_client.publish.return_value = mock_future

        await pub.publish_run_started(event)
        mock_client.publish.assert_called_once()
        call_kwargs = mock_client.publish.call_args
        assert call_kwargs[1]["event_type"] == "run.started"

    async def test_publish_heartbeat(self):
        """publish_heartbeat publishes run.heartbeat event."""
        pub, mock_client = _make_pubsub_publisher()

        mock_future = asyncio.Future()
        mock_future.set_result("msg-id")
        mock_client.publish.return_value = mock_future

        await pub.publish_heartbeat("run-1", root_deployment_id="root-1", span_id="span-1")
        mock_client.publish.assert_called_once()
        call_kwargs = mock_client.publish.call_args
        assert call_kwargs[1]["event_type"] == "run.heartbeat"
        assert call_kwargs[1]["root_deployment_id"] == "root-1"

    async def test_heartbeat_contains_timestamp(self):
        """publish_heartbeat includes a timestamp field in the data payload."""
        pub, mock_client = _make_pubsub_publisher()

        mock_future = asyncio.Future()
        mock_future.set_result("msg-id")
        mock_client.publish.return_value = mock_future

        await pub.publish_heartbeat("run-1", root_deployment_id="root-1", span_id="span-1")
        published_data = mock_client.publish.call_args[0][1]
        envelope = json.loads(published_data)
        assert "timestamp" in envelope["data"]
        # Verify it's an ISO format string
        assert "T" in envelope["data"]["timestamp"]
        assert envelope["data"]["root_deployment_id"] == "root-1"

    async def test_publish_completed_size_guard(self):
        """publish_run_completed raises ResultTooLargeError for oversized messages."""
        pub, mock_client = _make_pubsub_publisher()

        huge_result = {"data": "x" * (MAX_PUBSUB_MESSAGE_BYTES + 1)}
        event = RunCompletedEvent(
            run_id="run-1",
            span_id="span-1",
            root_deployment_id="root-1",
            parent_deployment_task_id=None,
            status="completed",
            result=huge_result,
        )

        with pytest.raises(ResultTooLargeError):
            await pub.publish_run_completed(event)

        mock_client.publish.assert_not_called()

    async def test_publish_failed(self):
        """publish_run_failed publishes run.failed event."""
        pub, mock_client = _make_pubsub_publisher()
        event = RunFailedEvent(
            run_id="run-1",
            span_id="span-1",
            root_deployment_id="root-1",
            parent_deployment_task_id=None,
            status="failed",
            error_code=ErrorCode.PIPELINE_ERROR,
            error_message="something broke",
        )

        mock_future = asyncio.Future()
        mock_future.set_result("msg-id")
        mock_client.publish.return_value = mock_future

        await pub.publish_run_failed(event)
        mock_client.publish.assert_called_once()
        call_kwargs = mock_client.publish.call_args
        assert call_kwargs[1]["event_type"] == "run.failed"

    def test_make_attributes(self):
        """_make_attributes returns correct Pub/Sub message attributes."""
        pub, _ = _make_pubsub_publisher()
        attrs = pub._make_attributes(EventType.RUN_STARTED, "run-1")
        assert attrs == {
            "service_type": "research",
            "event_type": "run.started",
            "run_id": "run-1",
        }

    async def test_publish_failure_logs_warning(self):
        """Publish failure is logged but does not raise (fire-and-forget)."""
        pub, mock_client = _make_pubsub_publisher()

        mock_future = asyncio.Future()
        mock_future.set_exception(RuntimeError("network error"))
        mock_client.publish.return_value = mock_future

        # Should not raise
        with patch("ai_pipeline_core.deployment._pubsub.asyncio.sleep", new_callable=AsyncMock):
            await pub.publish_heartbeat("run-1", root_deployment_id="root-1", span_id="span-1")

    async def test_publish_event_raises_when_root_deployment_id_missing(self):
        """_publish_event rejects missing root_deployment_id instead of silently dropping the attribute."""
        pub, mock_client = _make_pubsub_publisher()

        with pytest.raises(ValueError, match="root_deployment_id missing"):
            await pub._publish_event(EventType.RUN_HEARTBEAT, "run-1", {"span_id": "span-1", "timestamp": "2026-01-01T00:00:00+00:00"})

        mock_client.publish.assert_not_called()

    async def test_publish_event_raises_when_root_deployment_id_is_empty(self):
        """_publish_event rejects empty root_deployment_id instead of publishing degraded attributes."""
        pub, mock_client = _make_pubsub_publisher()

        with pytest.raises(ValueError, match="root_deployment_id missing"):
            await pub._publish_event(
                EventType.RUN_HEARTBEAT,
                "run-1",
                {"root_deployment_id": "", "span_id": "span-1", "timestamp": "2026-01-01T00:00:00+00:00"},
            )

        mock_client.publish.assert_not_called()

    async def test_retries_on_failure(self):
        """Publish retries with exponential backoff."""
        pub, mock_client = _make_pubsub_publisher()

        # First call fails, second succeeds
        fail_future = asyncio.Future()
        fail_future.set_exception(RuntimeError("temporary failure"))
        success_future = asyncio.Future()
        success_future.set_result("msg-id")
        mock_client.publish.side_effect = [fail_future, success_future]

        event = RunStartedEvent(
            run_id="run-1",
            span_id="span-1",
            root_deployment_id="root-1",
            parent_deployment_task_id=None,
            input_fingerprint="abc123456789abcd",
            status="running",
        )

        with patch("ai_pipeline_core.deployment._pubsub.asyncio.sleep", new_callable=AsyncMock):
            await pub.publish_run_started(event)

        assert mock_client.publish.call_count == 2

    async def test_exhausts_retries_without_raising(self):
        """Publish logs warning after exhausting all retries (fire-and-forget)."""
        pub, mock_client = _make_pubsub_publisher()

        # All calls fail
        def make_fail_future(*args, **kwargs):
            f = asyncio.Future()
            f.set_exception(RuntimeError("persistent failure"))
            return f

        mock_client.publish.side_effect = make_fail_future

        event = RunStartedEvent(
            run_id="run-1",
            span_id="span-1",
            root_deployment_id="root-1",
            parent_deployment_task_id=None,
            input_fingerprint="abc123456789abcd",
            status="running",
        )

        with patch("ai_pipeline_core.deployment._pubsub.asyncio.sleep", new_callable=AsyncMock):
            # Should not raise — fire-and-forget on final failure
            await pub.publish_run_started(event)

        assert mock_client.publish.call_count == MAX_RETRIES
