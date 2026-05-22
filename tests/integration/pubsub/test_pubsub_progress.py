"""Tests for Pub/Sub heartbeat events.

Verifies that heartbeat publishing works across publisher implementations
and that the CloudEvents envelope structure is correct.
"""

# pyright: reportPrivateUsage=false

import json
from unittest.mock import AsyncMock, patch

import pytest

from ai_pipeline_core.deployment._pubsub import (
    CLOUDEVENTS_SPEC_VERSION,
    PubSubPublisher,
)
from ai_pipeline_core.deployment._types import (
    EventType,
    _MemoryPublisher,
    _NoopPublisher,
)


class TestNoopPublisherHeartbeat:
    """Verify _NoopPublisher.publish_heartbeat() completes without error."""

    async def test_noop_heartbeat_completes(self) -> None:
        """_NoopPublisher silently discards heartbeat calls."""
        publisher = _NoopPublisher()
        await publisher.publish_heartbeat("run-123", root_deployment_id="root-123", span_id="span-123")

    async def test_noop_heartbeat_accepts_any_run_id(self) -> None:
        """_NoopPublisher accepts arbitrary run_id values when routing fields are present."""
        publisher = _NoopPublisher()
        await publisher.publish_heartbeat("", root_deployment_id="root-empty", span_id="span-empty")
        await publisher.publish_heartbeat("run-with-special-chars-!@#$", root_deployment_id="root-special", span_id="span-special")


class TestMemoryPublisherHeartbeat:
    """Verify _MemoryPublisher records heartbeat run_ids."""

    async def test_memory_publisher_records_heartbeat(self) -> None:
        """_MemoryPublisher appends heartbeat dict to heartbeats list."""
        publisher = _MemoryPublisher()
        await publisher.publish_heartbeat("run-abc", root_deployment_id="root-abc", span_id="span-abc")
        assert len(publisher.heartbeats) == 1
        assert publisher.heartbeats[0]["run_id"] == "run-abc"
        assert publisher.heartbeats[0]["root_deployment_id"] == "root-abc"
        assert publisher.heartbeats[0]["span_id"] == "span-abc"

    async def test_memory_publisher_records_multiple_heartbeats(self) -> None:
        """Multiple heartbeat calls accumulate in order."""
        publisher = _MemoryPublisher()
        await publisher.publish_heartbeat("run-1", root_deployment_id="root-1", span_id="span-1")
        await publisher.publish_heartbeat("run-2", root_deployment_id="root-2", span_id="span-2")
        await publisher.publish_heartbeat("run-1", root_deployment_id="root-1b", span_id="span-1b")
        assert len(publisher.heartbeats) == 3
        assert publisher.heartbeats[0]["run_id"] == "run-1"
        assert publisher.heartbeats[1]["run_id"] == "run-2"
        assert publisher.heartbeats[2]["run_id"] == "run-1"
        assert publisher.heartbeats[0]["root_deployment_id"] == "root-1"
        assert publisher.heartbeats[1]["root_deployment_id"] == "root-2"
        assert publisher.heartbeats[2]["root_deployment_id"] == "root-1b"


class TestHeartbeatCloudEventsEnvelope:
    """Verify the CloudEvents 1.0 envelope structure for heartbeat events."""

    @pytest.fixture
    def publisher(self) -> PubSubPublisher:
        """Create a PubSubPublisher with a mocked Pub/Sub client."""
        with patch("ai_pipeline_core.deployment._pubsub.PublisherClient") as mock_cls:
            mock_client = mock_cls.return_value
            mock_client.topic_path.return_value = "projects/test/topics/test-topic"
            pub = PubSubPublisher(
                project_id="test-project",
                topic_id="test-topic",
                service_type="analyzer",
            )
        return pub

    def test_heartbeat_envelope_has_cloudevents_fields(self, publisher: PubSubPublisher) -> None:
        """_build_envelope for heartbeat contains all required CloudEvents 1.0 fields."""
        run_id = "run-envelope-test"
        data = publisher._build_envelope(
            EventType.RUN_HEARTBEAT,
            run_id,
            {"timestamp": "2026-03-08T00:00:00+00:00"},
        )
        envelope = json.loads(data)

        assert envelope["specversion"] == CLOUDEVENTS_SPEC_VERSION
        assert envelope["type"] == EventType.RUN_HEARTBEAT
        assert envelope["subject"] == run_id
        assert envelope["source"] == "ai-analyzer-worker"
        assert envelope["datacontenttype"] == "application/json"
        # id and time must be present
        assert "id" in envelope
        assert "time" in envelope

    def test_heartbeat_envelope_data_contains_run_id(self, publisher: PubSubPublisher) -> None:
        """The data payload includes run_id and timestamp."""
        run_id = "run-data-test"
        timestamp = "2026-03-08T12:00:00+00:00"
        data = publisher._build_envelope(
            EventType.RUN_HEARTBEAT,
            run_id,
            {"timestamp": timestamp},
        )
        envelope = json.loads(data)

        assert envelope["data"]["run_id"] == run_id
        assert envelope["data"]["timestamp"] == timestamp

    def test_heartbeat_attributes_contain_event_type_and_run_id(self, publisher: PubSubPublisher) -> None:
        """_make_attributes returns correct service_type, event_type, and run_id."""
        run_id = "run-attrs-test"
        attrs = publisher._make_attributes(EventType.RUN_HEARTBEAT, run_id)

        assert attrs["event_type"] == str(EventType.RUN_HEARTBEAT)
        assert attrs["run_id"] == run_id
        assert attrs["service_type"] == "analyzer"

    async def test_publish_heartbeat_calls_publish_with_correct_envelope(self, publisher: PubSubPublisher) -> None:
        """publish_heartbeat() builds envelope with RUN_HEARTBEAT type and publishes it."""
        publisher._publish = AsyncMock()  # type: ignore[assignment]

        await publisher.publish_heartbeat("run-full-test", root_deployment_id="root-full-test", span_id="span-full-test")

        publisher._publish.assert_called_once()  # type: ignore[union-attr]
        call_args = publisher._publish.call_args  # type: ignore[union-attr]

        # First arg is the envelope bytes
        envelope = json.loads(call_args[0][0])
        assert envelope["type"] == EventType.RUN_HEARTBEAT
        assert envelope["subject"] == "run-full-test"
        assert envelope["data"]["run_id"] == "run-full-test"
        assert envelope["data"]["root_deployment_id"] == "root-full-test"
        assert envelope["data"]["span_id"] == "span-full-test"
        assert "timestamp" in envelope["data"]

        # Second arg is the attributes dict
        attrs = call_args[0][1]
        assert attrs["event_type"] == str(EventType.RUN_HEARTBEAT)
        assert attrs["run_id"] == "run-full-test"
        assert attrs["root_deployment_id"] == "root-full-test"
