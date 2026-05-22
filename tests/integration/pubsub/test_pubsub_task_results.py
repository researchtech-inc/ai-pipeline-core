"""Tests for task lifecycle events — TaskStartedEvent, TaskCompletedEvent, TaskFailedEvent.

Verifies event construction with new fields (span_id, root_deployment_id,
parent_deployment_task_id, status), _MemoryPublisher recording, and PubSubPublisher
envelope generation.
"""

# pyright: reportPrivateUsage=false

import json
from dataclasses import asdict
from unittest.mock import AsyncMock, patch

import pytest

from ai_pipeline_core.deployment._pubsub import PubSubPublisher
from ai_pipeline_core.deployment._types import (
    DocumentRef,
    EventType,
    TaskCompletedEvent,
    TaskFailedEvent,
    TaskStartedEvent,
    _MemoryPublisher,
)

NODE_ID = "node-abc-123"
ROOT_DEPLOYMENT_ID = "root-deploy-001"
PARENT_DEPLOYMENT_TASK_ID = "parent-task-xyz"
RUN_ID = "run-42"
FLOW_NAME = "analyze"
STEP = 2
TASK_NAME = "extract_entities"
TASK_CLASS = "ExtractEntitiesTask"
TASK_RUNNING_STATUS = "running"
TASK_COMPLETED_STATUS = "completed"
TASK_FAILED_STATUS = "failed"


TOTAL_STEPS = 3


def _make_started_event(*, parent: str | None = PARENT_DEPLOYMENT_TASK_ID) -> TaskStartedEvent:
    return TaskStartedEvent(
        run_id=RUN_ID,
        span_id=NODE_ID,
        root_deployment_id=ROOT_DEPLOYMENT_ID,
        parent_deployment_task_id=parent,
        flow_name=FLOW_NAME,
        step=STEP,
        total_steps=TOTAL_STEPS,
        status=TASK_RUNNING_STATUS,
        task_name=TASK_NAME,
        task_class=TASK_CLASS,
    )


def _make_completed_event(
    *,
    parent: str | None = PARENT_DEPLOYMENT_TASK_ID,
    duration_ms: int = 1500,
    output_documents: list[DocumentRef] | None = None,
) -> TaskCompletedEvent:
    return TaskCompletedEvent(
        run_id=RUN_ID,
        span_id=NODE_ID,
        root_deployment_id=ROOT_DEPLOYMENT_ID,
        parent_deployment_task_id=parent,
        flow_name=FLOW_NAME,
        step=STEP,
        total_steps=TOTAL_STEPS,
        status=TASK_COMPLETED_STATUS,
        task_name=TASK_NAME,
        task_class=TASK_CLASS,
        duration_ms=duration_ms,
        output_documents=tuple(output_documents or []),
    )


def _make_failed_event(
    *,
    parent: str | None = PARENT_DEPLOYMENT_TASK_ID,
    error_message: str = "something went wrong",
) -> TaskFailedEvent:
    return TaskFailedEvent(
        run_id=RUN_ID,
        span_id=NODE_ID,
        root_deployment_id=ROOT_DEPLOYMENT_ID,
        parent_deployment_task_id=parent,
        flow_name=FLOW_NAME,
        step=STEP,
        total_steps=TOTAL_STEPS,
        status=TASK_FAILED_STATUS,
        task_name=TASK_NAME,
        task_class=TASK_CLASS,
        error_message=error_message,
    )


class TestTaskEventConstruction:
    """Task events carry span_id, root_deployment_id, parent_deployment_task_id, and status."""

    def test_started_event_has_new_fields(self):
        event = _make_started_event()
        assert event.span_id == NODE_ID
        assert event.root_deployment_id == ROOT_DEPLOYMENT_ID
        assert event.parent_deployment_task_id == PARENT_DEPLOYMENT_TASK_ID
        assert event.run_id == RUN_ID
        assert event.flow_name == FLOW_NAME
        assert event.step == STEP
        assert event.status == TASK_RUNNING_STATUS
        assert event.task_name == TASK_NAME
        assert event.task_class == TASK_CLASS

    def test_completed_event_has_new_fields_and_duration(self):
        doc = DocumentRef(sha256="abc123", class_name="Report", name="report.md")
        event = _make_completed_event(duration_ms=2000, output_documents=[doc])
        assert event.span_id == NODE_ID
        assert event.root_deployment_id == ROOT_DEPLOYMENT_ID
        assert event.parent_deployment_task_id == PARENT_DEPLOYMENT_TASK_ID
        assert event.status == TASK_COMPLETED_STATUS
        assert event.duration_ms == 2000
        assert len(event.output_documents) == 1
        assert event.output_documents[0].sha256 == "abc123"

    def test_failed_event_has_new_fields_and_error(self):
        event = _make_failed_event(error_message="timeout reached")
        assert event.span_id == NODE_ID
        assert event.root_deployment_id == ROOT_DEPLOYMENT_ID
        assert event.parent_deployment_task_id == PARENT_DEPLOYMENT_TASK_ID
        assert event.status == TASK_FAILED_STATUS
        assert event.error_message == "timeout reached"

    def test_parent_deployment_task_id_can_be_none(self):
        started = _make_started_event(parent=None)
        completed = _make_completed_event(parent=None)
        failed = _make_failed_event(parent=None)
        assert started.parent_deployment_task_id is None
        assert completed.parent_deployment_task_id is None
        assert failed.parent_deployment_task_id is None

    def test_task_events_are_frozen(self):
        event = _make_started_event()
        with pytest.raises(AttributeError):
            event.task_name = "other"  # type: ignore[misc]

    def test_asdict_roundtrip(self):
        event = _make_completed_event(
            output_documents=[DocumentRef(sha256="def456", class_name="Summary", name="summary.md")],
        )
        d = asdict(event)
        assert d["span_id"] == NODE_ID
        assert d["root_deployment_id"] == ROOT_DEPLOYMENT_ID
        assert d["parent_deployment_task_id"] == PARENT_DEPLOYMENT_TASK_ID
        assert d["status"] == TASK_COMPLETED_STATUS
        assert d["output_documents"][0]["sha256"] == "def456"


class TestMemoryPublisherTaskEvents:
    """_MemoryPublisher records task events for test assertions."""

    async def test_publish_task_started_records_event(self):
        publisher = _MemoryPublisher()
        event = _make_started_event()
        await publisher.publish_task_started(event)
        assert len(publisher.events) == 1
        assert publisher.events[0] is event

    async def test_publish_task_completed_records_event(self):
        publisher = _MemoryPublisher()
        event = _make_completed_event()
        await publisher.publish_task_completed(event)
        assert len(publisher.events) == 1
        assert isinstance(publisher.events[0], TaskCompletedEvent)
        assert publisher.events[0].duration_ms == 1500

    async def test_publish_task_failed_records_event(self):
        publisher = _MemoryPublisher()
        event = _make_failed_event()
        await publisher.publish_task_failed(event)
        assert len(publisher.events) == 1
        assert isinstance(publisher.events[0], TaskFailedEvent)
        assert publisher.events[0].error_message == "something went wrong"

    async def test_multiple_task_events_recorded_in_order(self):
        publisher = _MemoryPublisher()
        started = _make_started_event()
        completed = _make_completed_event()
        failed = _make_failed_event()
        await publisher.publish_task_started(started)
        await publisher.publish_task_completed(completed)
        await publisher.publish_task_failed(failed)
        assert len(publisher.events) == 3
        assert isinstance(publisher.events[0], TaskStartedEvent)
        assert isinstance(publisher.events[1], TaskCompletedEvent)
        assert isinstance(publisher.events[2], TaskFailedEvent)


class TestPubSubPublisherTaskEnvelopes:
    """PubSubPublisher.publish_task_* produce correct CloudEvents envelopes."""

    @pytest.fixture
    def publisher_and_publish(self):
        """Create a PubSubPublisher with mocked Pub/Sub client, return (publisher, mock_publish)."""
        with patch("ai_pipeline_core.deployment._pubsub.PublisherClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.topic_path.return_value = "projects/test/topics/test-topic"
            publisher = PubSubPublisher(
                project_id="test-project",
                topic_id="test-topic",
                service_type="test-service",
            )
            # Replace _publish with an AsyncMock to capture calls without real Pub/Sub
            publisher._publish = AsyncMock()  # type: ignore[assignment]
            yield publisher, publisher._publish

    async def test_task_started_envelope(self, publisher_and_publish):
        publisher, mock_publish = publisher_and_publish
        event = _make_started_event()
        await publisher.publish_task_started(event)

        mock_publish.assert_awaited_once()
        data_bytes, attributes = mock_publish.call_args.args
        envelope = json.loads(data_bytes)

        assert envelope["type"] == EventType.TASK_STARTED
        assert envelope["subject"] == RUN_ID
        assert envelope["specversion"] == "1.0"
        assert envelope["data"]["span_id"] == NODE_ID
        assert envelope["data"]["root_deployment_id"] == ROOT_DEPLOYMENT_ID
        assert envelope["data"]["parent_deployment_task_id"] == PARENT_DEPLOYMENT_TASK_ID
        assert envelope["data"]["flow_name"] == FLOW_NAME
        assert envelope["data"]["step"] == STEP
        assert envelope["data"]["status"] == TASK_RUNNING_STATUS
        assert envelope["data"]["task_name"] == TASK_NAME
        assert envelope["data"]["task_class"] == TASK_CLASS
        assert attributes["event_type"] == str(EventType.TASK_STARTED)
        assert attributes["run_id"] == RUN_ID

    async def test_task_completed_envelope_with_documents(self, publisher_and_publish):
        publisher, mock_publish = publisher_and_publish
        doc = DocumentRef(sha256="sha-abc", class_name="Report", name="report.md", summary="A report")
        event = _make_completed_event(duration_ms=3000, output_documents=[doc])
        await publisher.publish_task_completed(event)

        mock_publish.assert_awaited_once()
        data_bytes, _ = mock_publish.call_args.args
        envelope = json.loads(data_bytes)

        assert envelope["type"] == EventType.TASK_COMPLETED
        assert envelope["data"]["span_id"] == NODE_ID
        assert envelope["data"]["root_deployment_id"] == ROOT_DEPLOYMENT_ID
        assert envelope["data"]["status"] == TASK_COMPLETED_STATUS
        assert envelope["data"]["duration_ms"] == 3000
        assert len(envelope["data"]["output_documents"]) == 1
        assert envelope["data"]["output_documents"][0]["sha256"] == "sha-abc"
        assert envelope["data"]["output_documents"][0]["summary"] == "A report"

    async def test_task_failed_envelope(self, publisher_and_publish):
        publisher, mock_publish = publisher_and_publish
        event = _make_failed_event(error_message="disk full")
        await publisher.publish_task_failed(event)

        mock_publish.assert_awaited_once()
        data_bytes, attributes = mock_publish.call_args.args
        envelope = json.loads(data_bytes)

        assert envelope["type"] == EventType.TASK_FAILED
        assert envelope["data"]["span_id"] == NODE_ID
        assert envelope["data"]["root_deployment_id"] == ROOT_DEPLOYMENT_ID
        assert envelope["data"]["parent_deployment_task_id"] == PARENT_DEPLOYMENT_TASK_ID
        assert envelope["data"]["status"] == TASK_FAILED_STATUS
        assert envelope["data"]["error_message"] == "disk full"
        assert attributes["event_type"] == str(EventType.TASK_FAILED)

    async def test_task_started_envelope_with_null_parent(self, publisher_and_publish):
        publisher, mock_publish = publisher_and_publish
        event = _make_started_event(parent=None)
        await publisher.publish_task_started(event)

        data_bytes, _ = mock_publish.call_args.args
        envelope = json.loads(data_bytes)
        assert envelope["data"]["parent_deployment_task_id"] is None
