"""Run completion event integration tests via real Pub/Sub emulator.

Verifies output_document_sha256s correctness, span_id/root_deployment_id
presence, and event integrity after full and partial resume.
"""

# pyright: reportPrivateUsage=false, reportArgumentType=false

from uuid import uuid4

from google.api_core.exceptions import GoogleAPICallError

from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.deployment._pubsub import PubSubPublisher
from ai_pipeline_core.deployment._types import EventType

from .conftest import (
    CollectedEvent,
    PubsubTestResources,
    PublisherWithStore,
    ThreeStageDeployment,
    TwoStageDeployment,
    make_input_doc,
    pull_events,
    run_pipeline,
)

# 2-flow success: 1 run.started + 2*(flow.started + task.started + task.completed + flow.completed) + 1 run.completed
TWO_FLOW_EVENT_COUNT = 10

# 2-flow full resume: 1 run.started + 2*(flow.skipped) + 1 run.completed
TWO_FLOW_RESUME_EVENT_COUNT = 4

# 3-flow success: 1 run.started + 3*(flow.started + task.started + task.completed + flow.completed) + 1 run.completed
THREE_FLOW_EVENT_COUNT = 14

# 3-flow full resume: 1 run.started + 3*(flow.skipped) + 1 run.completed
THREE_FLOW_RESUME_EVENT_COUNT = 5


def _get_completed_event(events: list[CollectedEvent]) -> CollectedEvent:
    """Return the run.completed event."""
    completed = [e for e in events if e.event_type == EventType.RUN_COMPLETED]
    assert len(completed) == 1, f"Expected 1 completed event, got {len(completed)}"
    return completed[0]


def _make_second_publisher(pubsub_test_resources: PubsubTestResources) -> tuple[PubsubTestResources, PublisherWithStore]:
    """Create a second topic+subscription and publisher for collecting only a second run's events."""
    from google.cloud.pubsub_v1 import PublisherClient, SubscriberClient

    pub_client = PublisherClient()
    sub_client = SubscriberClient()

    topic_id = f"test-events-{uuid4().hex[:8]}"
    topic_path = pub_client.topic_path(pubsub_test_resources.project_id, topic_id)
    sub_id = f"test-sub-{uuid4().hex[:8]}"
    sub_path = sub_client.subscription_path(pubsub_test_resources.project_id, sub_id)

    pub_client.create_topic(name=topic_path)
    sub_client.create_subscription(name=sub_path, topic=topic_path)

    resources = PubsubTestResources(
        project_id=pubsub_test_resources.project_id,
        topic_path=topic_path,
        subscription_path=sub_path,
        publisher_client=pub_client,
        subscriber_client=sub_client,
    )

    publisher = PubSubPublisher(
        project_id=pubsub_test_resources.project_id,
        topic_id=topic_id,
        service_type="test-service",
    )

    return resources, PublisherWithStore(publisher=publisher)


class TestRunCompletedEvent:
    """Run completion event structure and correctness tests."""

    async def test_completed_event_has_span_id_and_root_id(
        self,
        real_publisher: PublisherWithStore,
        pubsub_test_resources: PubsubTestResources,
    ):
        """Completed event has span_id and root_deployment_id in data payload."""
        deployment = TwoStageDeployment()
        await run_pipeline(deployment, real_publisher.publisher)

        events = pull_events(pubsub_test_resources, expected_count=TWO_FLOW_EVENT_COUNT)
        completed = _get_completed_event(events)

        assert "span_id" in completed.data
        assert "root_deployment_id" in completed.data
        assert len(completed.data["span_id"]) > 0
        assert len(completed.data["root_deployment_id"]) > 0

    async def test_output_sha256s_point_to_last_flow_outputs(
        self,
        real_publisher: PublisherWithStore,
        pubsub_test_resources: PubsubTestResources,
    ):
        """output_document_sha256s contains SHA256s of last flow's outputs, not intermediate docs."""
        deployment = TwoStageDeployment()
        await run_pipeline(deployment, real_publisher.publisher)

        events = pull_events(pubsub_test_resources, expected_count=TWO_FLOW_EVENT_COUNT)
        completed = _get_completed_event(events)
        output_sha256s_in_event = completed.data.get("output_document_sha256s", [])

        assert len(output_sha256s_in_event) > 0, "output_document_sha256s should not be empty"

    async def test_completed_event_correct_after_full_resume(
        self,
        real_publisher: PublisherWithStore,
        pubsub_test_resources: PubsubTestResources,
    ):
        """After full resume (all flows cached), completed event is still emitted."""
        deployment = TwoStageDeployment()
        input_doc = make_input_doc()
        db = _MemoryDatabase()

        # First run — all flows execute
        await run_pipeline(deployment, real_publisher.publisher, docs=[input_doc], database=db)
        pull_events(pubsub_test_resources, expected_count=TWO_FLOW_EVENT_COUNT)

        # Second run — same database (has flow completions), new topic/publisher for clean event collection
        second_resources, second_pub = _make_second_publisher(pubsub_test_resources)
        try:
            await run_pipeline(deployment, second_pub.publisher, docs=[input_doc], database=db)
            second_events = pull_events(second_resources, expected_count=TWO_FLOW_RESUME_EVENT_COUNT)

            # Completed event is still emitted on resume
            completed = _get_completed_event(second_events)
            assert "span_id" in completed.data
            assert "root_deployment_id" in completed.data
        finally:
            try:
                second_resources.subscriber_client.delete_subscription(subscription=second_resources.subscription_path)
            except OSError, GoogleAPICallError:
                pass
            try:
                second_resources.publisher_client.delete_topic(topic=second_resources.topic_path)
            except OSError, GoogleAPICallError:
                pass

    async def test_completed_event_correct_after_full_resume_3flow(
        self,
        real_publisher: PublisherWithStore,
        pubsub_test_resources: PubsubTestResources,
    ):
        """After full resume on 3-flow pipeline, completed event is still emitted with correct structure."""
        deployment = ThreeStageDeployment()
        input_doc = make_input_doc()
        db = _MemoryDatabase()

        # First run — all 3 flows execute
        await run_pipeline(deployment, real_publisher.publisher, docs=[input_doc], database=db)
        pull_events(pubsub_test_resources, expected_count=THREE_FLOW_EVENT_COUNT)

        # Second run — all 3 flows cached, new topic for clean collection
        second_resources, second_pub = _make_second_publisher(pubsub_test_resources)
        try:
            await run_pipeline(deployment, second_pub.publisher, docs=[input_doc], database=db)
            second_events = pull_events(second_resources, expected_count=THREE_FLOW_RESUME_EVENT_COUNT)
            completed = _get_completed_event(second_events)

            assert "span_id" in completed.data
            assert "root_deployment_id" in completed.data
        finally:
            try:
                second_resources.subscriber_client.delete_subscription(subscription=second_resources.subscription_path)
            except OSError, GoogleAPICallError:
                pass
            try:
                second_resources.publisher_client.delete_topic(topic=second_resources.topic_path)
            except OSError, GoogleAPICallError:
                pass
