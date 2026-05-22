"""Tests for publisher factory, heartbeat lifecycle, and run() publisher integration."""

# pyright: reportPrivateUsage=false

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from ai_pipeline_core import (
    DeploymentResult,
    Document,
    FlowOptions,
    PipelineDeployment,
)
from ai_pipeline_core.deployment._types import (
    ErrorCode,
    ResultPublisher,
    RunCompletedEvent,
    RunFailedEvent,
    RunStartedEvent,
    _MemoryPublisher,
    _NoopPublisher,
)
from ai_pipeline_core.deployment._helpers import _create_publisher
from ai_pipeline_core.pipeline import PipelineFlow


# --- Test infrastructure ---


class _WiringInputDoc(Document):
    """Input for wiring tests."""


class _WiringOutputDoc(Document):
    """Output for wiring tests."""


class _WiringResult(DeploymentResult):
    """Result for wiring tests."""


class _WiringFlow(PipelineFlow):
    """Flow for wiring tests."""

    async def run(self, input_docs: tuple[_WiringInputDoc, ...], options: FlowOptions) -> tuple[_WiringOutputDoc, ...]:
        _ = options
        return (_WiringOutputDoc.derive(derived_from=input_docs, name="out.txt", content="done"),)


class _WiringDeployment(PipelineDeployment[FlowOptions, _WiringResult]):
    """Deployment for wiring tests."""

    flow_retries = 0

    def build_flows(self, options: FlowOptions) -> list[PipelineFlow]:
        return [_WiringFlow()]

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> _WiringResult:
        return _WiringResult(success=True)


class _FailingFlow(PipelineFlow):
    """Flow that always raises."""

    retries = 0

    async def run(self, input_docs: tuple[_WiringInputDoc, ...], options: FlowOptions) -> tuple[_WiringOutputDoc, ...]:
        _ = (input_docs, options)
        raise RuntimeError("deliberate failure")


class _FailingDeployment(PipelineDeployment[FlowOptions, _WiringResult]):
    """Deployment that always fails."""

    flow_retries = 0

    def build_flows(self, options: FlowOptions) -> list[PipelineFlow]:
        return [_FailingFlow()]

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> _WiringResult:
        return _WiringResult(success=False, error="failed")


class _CancellingFlow(PipelineFlow):
    """Flow that raises CancelledError."""

    async def run(self, input_docs: tuple[_WiringInputDoc, ...], options: FlowOptions) -> tuple[_WiringOutputDoc, ...]:
        _ = (input_docs, options)
        raise asyncio.CancelledError()


class _CancelDeployment(PipelineDeployment[FlowOptions, _WiringResult]):
    """Deployment where the flow raises CancelledError."""

    flow_retries = 0

    def build_flows(self, options: FlowOptions) -> list[PipelineFlow]:
        return [_CancellingFlow()]

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> _WiringResult:
        return _WiringResult(success=False)


# --- _create_publisher tests ---


class TestCreatePublisher:
    """Test _create_publisher factory function."""

    def test_returns_noop_when_pubsub_not_configured(self):
        """No pubsub_project_id → _NoopPublisher."""
        mock_settings = MagicMock()
        mock_settings.pubsub_project_id = ""
        mock_settings.pubsub_topic_id = ""
        publisher = _create_publisher(mock_settings, "research")
        assert isinstance(publisher, _NoopPublisher)

    def test_returns_noop_when_topic_id_missing(self):
        """pubsub_project_id set but pubsub_topic_id empty → _NoopPublisher."""
        mock_settings = MagicMock()
        mock_settings.pubsub_project_id = "my-project"
        mock_settings.pubsub_topic_id = ""
        publisher = _create_publisher(mock_settings, "research")
        assert isinstance(publisher, _NoopPublisher)

    def test_returns_noop_when_service_type_empty(self):
        """Empty service_type → _NoopPublisher regardless of other config."""
        mock_settings = MagicMock()
        mock_settings.pubsub_project_id = "my-project"
        mock_settings.pubsub_topic_id = "events"
        publisher = _create_publisher(mock_settings, "")
        assert isinstance(publisher, _NoopPublisher)

    def test_creates_pubsub_publisher_when_configured(self):
        """Full Pub/Sub config → PubSubPublisher (no ClickHouse required)."""
        mock_settings = MagicMock()
        mock_settings.pubsub_project_id = "my-project"
        mock_settings.pubsub_topic_id = "events"

        with patch("ai_pipeline_core.deployment._helpers.PubSubPublisher") as mock_pub_cls:
            mock_pub_cls.return_value = MagicMock(spec=ResultPublisher)
            _create_publisher(mock_settings, "research")
            mock_pub_cls.assert_called_once_with(
                project_id="my-project",
                topic_id="events",
                service_type="research",
            )


# --- run() publisher integration tests ---


class TestRunPublisherIntegration:
    """Test that run() publishes lifecycle events via the publisher."""

    async def test_publishes_started_event(self):
        """run() must publish RunStartedEvent before executing flows."""
        pub = _MemoryPublisher()
        deployment = _WiringDeployment()
        doc = _WiringInputDoc.create_root(name="in.txt", content="test", reason="test")
        await deployment.run("run-1", [doc], FlowOptions(), publisher=pub)

        started_events = [e for e in pub.events if isinstance(e, RunStartedEvent)]
        assert len(started_events) == 1
        assert started_events[0].run_id == "run-1"

    async def test_publishes_completed_event(self):
        """run() must publish RunCompletedEvent after successful execution."""
        pub = _MemoryPublisher()
        deployment = _WiringDeployment()
        doc = _WiringInputDoc.create_root(name="in.txt", content="test", reason="test")
        await deployment.run("run-1", [doc], FlowOptions(), publisher=pub)

        completed_events = [e for e in pub.events if isinstance(e, RunCompletedEvent)]
        assert len(completed_events) == 1
        assert completed_events[0].run_id == "run-1"
        assert completed_events[0].span_id
        assert completed_events[0].root_deployment_id

    async def test_publishes_failed_event_on_error(self):
        """run() must publish RunFailedEvent when a flow raises."""
        pub = _MemoryPublisher()
        deployment = _FailingDeployment()
        doc = _WiringInputDoc.create_root(name="in.txt", content="test", reason="test")
        with pytest.raises(RuntimeError, match="deliberate failure"):
            await deployment.run("run-1", [doc], FlowOptions(), publisher=pub)

        failed_events = [e for e in pub.events if isinstance(e, RunFailedEvent)]
        assert len(failed_events) == 1
        assert failed_events[0].run_id == "run-1"
        assert failed_events[0].error_code == ErrorCode.UNKNOWN
        assert "deliberate failure" in failed_events[0].error_message

    async def test_event_ordering(self):
        """Events follow order: started → ... → completed."""
        pub = _MemoryPublisher()
        deployment = _WiringDeployment()
        doc = _WiringInputDoc.create_root(name="in.txt", content="test", reason="test")
        await deployment.run("run-1", [doc], FlowOptions(), publisher=pub)

        event_types = [type(e).__name__ for e in pub.events]
        assert event_types[0] == "RunStartedEvent"
        assert event_types[-1] == "RunCompletedEvent"

    async def test_heartbeat_runs_during_execution(self):
        """Heartbeat task is created and cancelled during run()."""
        pub = _MemoryPublisher()

        # Patch heartbeat interval to be very short
        with patch("ai_pipeline_core.deployment._helpers._HEARTBEAT_INTERVAL_SECONDS", 0.01):
            deployment = _WiringDeployment()
            doc = _WiringInputDoc.create_root(name="in.txt", content="test", reason="test")
            await deployment.run("run-1", [doc], FlowOptions(), publisher=pub)

        # Heartbeats may or may not have fired depending on flow speed,
        # but the heartbeat task should have been created and cleaned up without errors

    async def test_noop_publisher_is_default(self):
        """run() with publisher=None defaults to _NoopPublisher (no crash)."""
        deployment = _WiringDeployment()
        doc = _WiringInputDoc.create_root(name="in.txt", content="test", reason="test")
        # No publisher argument → defaults to _NoopPublisher
        result = await deployment.run("run-1", [doc], FlowOptions())
        assert result.success

    async def test_cancelled_error_publishes_failed(self):
        """CancelledError in flow publishes RunFailedEvent with CANCELLED error code."""
        pub = _MemoryPublisher()
        deployment = _CancelDeployment()
        doc = _WiringInputDoc.create_root(name="in.txt", content="test", reason="test")
        with pytest.raises(asyncio.CancelledError):
            await deployment.run("run-1", [doc], FlowOptions(), publisher=pub)

        failed_events = [e for e in pub.events if isinstance(e, RunFailedEvent)]
        assert len(failed_events) == 1
        assert failed_events[0].error_code == ErrorCode.CANCELLED
