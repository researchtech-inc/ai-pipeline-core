"""Tests for resume/cached flow behavior with real Pub/Sub emulator.

Verifies that resumed (cached) flows publish correct flow.skipped events
with proper step numbering.
"""

# pyright: reportPrivateUsage=false, reportArgumentType=false

from google.api_core.exceptions import GoogleAPICallError

from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.deployment._pubsub import PubSubPublisher
from ai_pipeline_core.deployment._types import EventType, _NoopPublisher

from .conftest import (
    CollectedEvent,
    PubsubTestResources,
    PublisherWithStore,
    SingleStageDeployment,
    ThreeStageDeployment,
    TwoStageDeployment,
    _flow_executions,
    make_input_doc,
    pull_events,
    run_pipeline,
)


def _skipped_events(events: list[CollectedEvent]) -> list[CollectedEvent]:
    """Filter to only flow.skipped events."""
    return [e for e in events if e.event_type == EventType.FLOW_SKIPPED]


def _make_fresh_publisher(pubsub_test_resources: PubsubTestResources) -> PublisherWithStore:
    """Create a new PubSubPublisher for the second run."""
    topic_id = pubsub_test_resources.topic_path.split("/")[-1]
    publisher = PubSubPublisher(
        project_id=pubsub_test_resources.project_id,
        topic_id=topic_id,
        service_type="test-service",
    )
    return PublisherWithStore(publisher=publisher)


class TestResumedFlowCachedStatus:
    """Verify that resumed flows publish flow.skipped events."""

    async def test_resumed_flow_publishes_cached_status(
        self,
        pubsub_emulator: str,
    ):
        """Run TwoStageDeployment twice; second run's skipped events have correct step numbering."""
        deployment = TwoStageDeployment()
        doc = make_input_doc()
        db = _MemoryDatabase()

        # First run: use _NoopPublisher (we don't care about its events)
        await run_pipeline(deployment, _NoopPublisher(), docs=[doc], database=db)
        assert len(_flow_executions) == 2  # both flows executed

        # Create a fresh topic + subscription for the second run
        from uuid import uuid4

        from google.cloud.pubsub_v1 import PublisherClient, SubscriberClient

        pub_client = PublisherClient()
        sub_client = SubscriberClient()
        topic_id = f"test-resume-{uuid4().hex[:8]}"
        topic_path = pub_client.topic_path("test-project", topic_id)
        sub_id = f"test-resume-sub-{uuid4().hex[:8]}"
        sub_path = sub_client.subscription_path("test-project", sub_id)
        pub_client.create_topic(name=topic_path)
        sub_client.create_subscription(name=sub_path, topic=topic_path)

        resources = PubsubTestResources(
            project_id="test-project",
            topic_path=topic_path,
            subscription_path=sub_path,
            publisher_client=pub_client,
            subscriber_client=sub_client,
        )

        try:
            second_pub = _make_fresh_publisher(resources)

            # Second run: same run_id, same docs, same database (flows should be cached)
            _flow_executions.clear()
            await run_pipeline(deployment, second_pub.publisher, docs=[doc], database=db)
            assert len(_flow_executions) == 0  # no flows executed (all cached)

            # 2 cached flows: 1 run.started + 2*flow.skipped + 1 run.completed = 4
            events = pull_events(resources, expected_count=4)
            skipped = _skipped_events(events)

            assert len(skipped) == 2

            # Step numbering is correct
            steps = sorted(evt.data["step"] for evt in skipped)
            assert steps == [1, 2]
        finally:
            try:
                sub_client.delete_subscription(subscription=sub_path)
            except (OSError, GoogleAPICallError):  # fmt: skip
                pass
            try:
                pub_client.delete_topic(topic=topic_path)
            except (OSError, GoogleAPICallError):  # fmt: skip
                pass


class TestResumedPipelineLifecycle:
    """Verify that fully cached pipelines still publish started + completed."""

    async def test_resumed_pipeline_still_publishes_started_and_completed(
        self,
        pubsub_emulator: str,
    ):
        """SingleStageDeployment run twice: second run still has run.started and run.completed."""
        deployment = SingleStageDeployment()
        doc = make_input_doc()
        db = _MemoryDatabase()

        # First run: use _NoopPublisher
        await run_pipeline(deployment, _NoopPublisher(), docs=[doc], database=db)
        assert len(_flow_executions) == 1

        # Create fresh topic for second run
        from uuid import uuid4

        from google.cloud.pubsub_v1 import PublisherClient, SubscriberClient

        pub_client = PublisherClient()
        sub_client = SubscriberClient()
        topic_id = f"test-resume-single-{uuid4().hex[:8]}"
        topic_path = pub_client.topic_path("test-project", topic_id)
        sub_id = f"test-resume-single-sub-{uuid4().hex[:8]}"
        sub_path = sub_client.subscription_path("test-project", sub_id)
        pub_client.create_topic(name=topic_path)
        sub_client.create_subscription(name=sub_path, topic=topic_path)

        resources = PubsubTestResources(
            project_id="test-project",
            topic_path=topic_path,
            subscription_path=sub_path,
            publisher_client=pub_client,
            subscriber_client=sub_client,
        )

        try:
            second_pub = _make_fresh_publisher(resources)

            _flow_executions.clear()
            await run_pipeline(deployment, second_pub.publisher, docs=[doc], database=db)
            assert len(_flow_executions) == 0  # cached

            # 1 cached flow: 1 run.started + flow.skipped + 1 run.completed = 3
            events = pull_events(resources, expected_count=3)

            started = [e for e in events if e.event_type == EventType.RUN_STARTED]
            completed = [e for e in events if e.event_type == EventType.RUN_COMPLETED]
            skipped = _skipped_events(events)

            assert len(started) == 1
            assert len(completed) == 1
            assert len(skipped) == 1
        finally:
            try:
                sub_client.delete_subscription(subscription=sub_path)
            except (OSError, GoogleAPICallError):  # fmt: skip
                pass
            try:
                pub_client.delete_topic(topic=topic_path)
            except (OSError, GoogleAPICallError):  # fmt: skip
                pass


class TestPartialResumeMix:
    """Verify fully cached multi-flow pipelines publish correct skipped events."""

    async def test_partial_resume_mix_of_cached_and_executed(
        self,
        pubsub_emulator: str,
    ):
        """3-flow deployment: all flows have completion records from first run,
        all get flow.skipped on second run.
        """
        deployment = ThreeStageDeployment()
        doc = make_input_doc()
        db = _MemoryDatabase()

        # First run to populate the database with documents and completions
        await run_pipeline(deployment, _NoopPublisher(), docs=[doc], database=db)
        assert set(_flow_executions) == {"flow_a", "flow_b", "flow_c"}

        # Create fresh topic for second run
        from uuid import uuid4

        from google.cloud.pubsub_v1 import PublisherClient, SubscriberClient

        pub_client = PublisherClient()
        sub_client = SubscriberClient()
        topic_id = f"test-partial-resume-{uuid4().hex[:8]}"
        topic_path = pub_client.topic_path("test-project", topic_id)
        sub_id = f"test-partial-resume-sub-{uuid4().hex[:8]}"
        sub_path = sub_client.subscription_path("test-project", sub_id)
        pub_client.create_topic(name=topic_path)
        sub_client.create_subscription(name=sub_path, topic=topic_path)

        resources = PubsubTestResources(
            project_id="test-project",
            topic_path=topic_path,
            subscription_path=sub_path,
            publisher_client=pub_client,
            subscriber_client=sub_client,
        )

        try:
            second_pub = _make_fresh_publisher(resources)

            _flow_executions.clear()
            await run_pipeline(deployment, second_pub.publisher, docs=[doc], database=db)

            # All flows should be cached (no re-execution)
            assert _flow_executions == []

            # 3 cached flows: 1 run.started + 3*flow.skipped + 1 run.completed = 5
            events = pull_events(resources, expected_count=5)

            skipped = _skipped_events(events)
            assert len(skipped) == 3

            # All three flows: skipped
            skipped_names = sorted(evt.data["flow_name"] for evt in skipped)
            assert "chain_input_to_middle" in skipped_names
            assert "chain_middle_to_output" in skipped_names
            assert "chain_output_to_final" in skipped_names

            # No flow.started or flow.completed events
            flow_started = [e for e in events if e.event_type == EventType.FLOW_STARTED]
            flow_completed = [e for e in events if e.event_type == EventType.FLOW_COMPLETED]
            assert len(flow_started) == 0
            assert len(flow_completed) == 0
        finally:
            try:
                sub_client.delete_subscription(subscription=sub_path)
            except (OSError, GoogleAPICallError):  # fmt: skip
                pass
            try:
                pub_client.delete_topic(topic=topic_path)
            except (OSError, GoogleAPICallError):  # fmt: skip
                pass
