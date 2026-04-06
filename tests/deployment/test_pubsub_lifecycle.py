"""Pub/Sub integration tests: full pipeline lifecycle event publishing.

Tests the core contract that consumers depend on — started, progress, completed/failed
events are published correctly through a real PubSubPublisher to the Pub/Sub emulator.
"""

# pyright: reportPrivateUsage=false, reportUnusedClass=false, reportArgumentType=false

import asyncio
from typing import Any

import pytest

from ai_pipeline_core import DeploymentResult, Document, FlowOptions, PipelineDeployment
from ai_pipeline_core.deployment._pubsub import PubSubPublisher, ResultTooLargeError
from ai_pipeline_core.deployment._types import ErrorCode, EventType
from ai_pipeline_core.pipeline import PipelineFlow

from .conftest import (
    FailingSecondStageDeployment,
    PubsubTestResources,
    PubsubInputDoc,
    PubsubOutputDoc,
    PubsubResult,
    PublisherWithStore,
    InputToMiddleFlow,
    MiddleToOutputFlow,
    TwoStageDeployment,
    _flow_executions,
    assert_seq_monotonic,
    assert_valid_cloudevent,
    pull_events,
    run_pipeline,
)

pytestmark = pytest.mark.pubsub


# ---------------------------------------------------------------------------
# Locally-defined deployments for specific test scenarios
# ---------------------------------------------------------------------------


class CancellingPubsubFlow(PipelineFlow):
    """Flow that raises CancelledError."""

    name = "cancelling_pubsub_flow"

    async def run(self, input_docs: tuple[PubsubInputDoc, ...], options: FlowOptions) -> tuple[PubsubOutputDoc, ...]:
        _flow_executions.append("cancelling_flow")
        _ = (input_docs, options)
        raise asyncio.CancelledError()


class CancellingDeployment(PipelineDeployment[FlowOptions, PubsubResult]):
    """Deployment where the first flow raises CancelledError."""

    flow_retries = 0

    def build_flows(self, options: FlowOptions) -> list[PipelineFlow]:
        return [CancellingPubsubFlow()]

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> PubsubResult:
        return PubsubResult(success=False, error="cancelled")


class BuildResultFailingDeployment(PipelineDeployment[FlowOptions, PubsubResult]):
    """Deployment where build_result raises ValueError after all flows succeed."""

    flow_retries = 0

    def build_flows(self, options: FlowOptions) -> list[PipelineFlow]:
        return [InputToMiddleFlow(), MiddleToOutputFlow()]

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> PubsubResult:
        raise ValueError("extraction failed")


class _OversizedResult(DeploymentResult):
    """Result with a very large field that exceeds the 8MB Pub/Sub limit."""

    huge_field: str = ""


class OversizedResultDeployment(PipelineDeployment[FlowOptions, _OversizedResult]):
    """Deployment whose build_result returns an oversized result."""

    flow_retries = 0

    def build_flows(self, options: FlowOptions) -> list[PipelineFlow]:
        return [InputToMiddleFlow(), MiddleToOutputFlow()]

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> _OversizedResult:
        return _OversizedResult(success=True, huge_field="x" * (9 * 1024 * 1024))


class FailingStartedPublisher(PubSubPublisher):
    """PubSubPublisher subclass that fails on publish_run_started."""

    async def publish_run_started(self, event: Any) -> None:
        raise RuntimeError("publish_started injection failure")


# ---------------------------------------------------------------------------
# Event count constants
# ---------------------------------------------------------------------------

# 2-flow success: 1 run.started + 2*(flow.started + task.started + task.completed + flow.completed) + 1 run.completed
TWO_FLOW_SUCCESS_EVENT_COUNT = 10

# 2-flow failure (flow 2 fails): 1 run.started
#   + (flow.started + task.started + task.completed + flow.completed) for flow 1
#   + (flow.started + flow.failed) for flow 2 + 1 run.failed
TWO_FLOW_FAILURE_EVENT_COUNT = 8

# Cancelled (1 flow): 1 run.started + flow.started + flow.failed + 1 run.failed
CANCELLED_EVENT_COUNT = 4

# build_result failure: same as success but run.failed instead of run.completed
BUILD_RESULT_FAILURE_EVENT_COUNT = 10


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPubsubLifecycle:
    """Full pipeline lifecycle event publishing through real Pub/Sub emulator."""

    async def test_successful_pipeline_publishes_complete_event_sequence(
        self,
        real_publisher: PublisherWithStore,
        pubsub_test_resources: PubsubTestResources,
    ):
        """2-flow success publishes run.started, flow events, progress events, and run.completed."""
        deployment = TwoStageDeployment()
        await run_pipeline(deployment, real_publisher.publisher)

        events = pull_events(pubsub_test_resources, expected_count=TWO_FLOW_SUCCESS_EVENT_COUNT)

        # First event is run.started, last is run.completed
        assert events[0].event_type == EventType.RUN_STARTED
        assert events[-1].event_type == EventType.RUN_COMPLETED

        # No failed events
        failed_events = [e for e in events if e.event_type == EventType.RUN_FAILED]
        assert len(failed_events) == 0

        # Seq values strictly increasing
        assert_seq_monotonic(events)

        # Exactly 1 terminal event (completed)
        terminal = [e for e in events if e.event_type in (EventType.RUN_COMPLETED, EventType.RUN_FAILED)]
        assert len(terminal) == 1

        # All events are valid CloudEvents
        for event in events:
            assert_valid_cloudevent(event)

        # Completed event contains correct result data
        completed_data = events[-1].data
        assert completed_data["result"]["success"] is True
        assert completed_data["result"]["doc_count"] > 0
        assert "output_document_sha256s" in completed_data

    async def test_failed_pipeline_publishes_started_and_failed(
        self,
        real_publisher: PublisherWithStore,
        pubsub_test_resources: PubsubTestResources,
    ):
        """Flow 2 raises RuntimeError: run.started + flow events + progress events + run.failed."""
        deployment = FailingSecondStageDeployment()
        with pytest.raises(RuntimeError, match="deliberate test failure"):
            await run_pipeline(deployment, real_publisher.publisher)

        events = pull_events(pubsub_test_resources, expected_count=TWO_FLOW_FAILURE_EVENT_COUNT)

        # run.started present
        started = [e for e in events if e.event_type == EventType.RUN_STARTED]
        assert len(started) == 1

        # run.failed present with correct error details
        failed = [e for e in events if e.event_type == EventType.RUN_FAILED]
        assert len(failed) == 1
        assert failed[0].data["error_code"] == ErrorCode.UNKNOWN
        assert "deliberate test failure" in failed[0].data["error_message"]

        flow_failed = [e for e in events if e.event_type == EventType.FLOW_FAILED]
        assert len(flow_failed) == 1
        assert flow_failed[0].data["flow_name"] == "failing_middle_to_output"
        assert "deliberate test failure" in flow_failed[0].data["error_message"]

        # NO run.completed event
        completed = [e for e in events if e.event_type == EventType.RUN_COMPLETED]
        assert len(completed) == 0

        assert_seq_monotonic(events)

    async def test_cancelled_pipeline_publishes_cancelled_error_code(
        self,
        real_publisher: PublisherWithStore,
        pubsub_test_resources: PubsubTestResources,
    ):
        """CancelledError in flow publishes run.failed with error_code=CANCELLED."""
        deployment = CancellingDeployment()
        with pytest.raises(asyncio.CancelledError):
            await run_pipeline(deployment, real_publisher.publisher)

        events = pull_events(pubsub_test_resources, expected_count=CANCELLED_EVENT_COUNT)

        failed = [e for e in events if e.event_type == EventType.RUN_FAILED]
        assert len(failed) == 1
        assert failed[0].data["error_code"] == ErrorCode.CANCELLED

        flow_failed = [e for e in events if e.event_type == EventType.FLOW_FAILED]
        assert len(flow_failed) == 1
        assert flow_failed[0].data["flow_name"] == "cancelling_pubsub_flow"

        assert_seq_monotonic(events)

    async def test_build_result_failure_publishes_failed_after_all_flows_complete(
        self,
        real_publisher: PublisherWithStore,
        pubsub_test_resources: PubsubTestResources,
    ):
        """build_result() raises ValueError: all flow progress events are published, then run.failed."""
        deployment = BuildResultFailingDeployment()
        with pytest.raises(ValueError, match="extraction failed"):
            await run_pipeline(deployment, real_publisher.publisher)

        events = pull_events(pubsub_test_resources, expected_count=BUILD_RESULT_FAILURE_EVENT_COUNT)

        # Failed event with correct error
        failed = [e for e in events if e.event_type == EventType.RUN_FAILED]
        assert len(failed) == 1
        assert failed[0].data["error_code"] == ErrorCode.INVALID_INPUT
        assert "extraction failed" in failed[0].data["error_message"]

        # NO run.completed event
        completed = [e for e in events if e.event_type == EventType.RUN_COMPLETED]
        assert len(completed) == 0

        # Verify both flows actually executed (work not lost despite build_result failure)
        assert "flow_1" in _flow_executions
        assert "flow_2" in _flow_executions

    async def test_result_too_large_publishes_failed_event(
        self,
        real_publisher: PublisherWithStore,
        pubsub_test_resources: PubsubTestResources,
    ):
        """Oversized build_result output: failed with PIPELINE_ERROR, no result in store."""
        deployment = OversizedResultDeployment()
        with pytest.raises(ResultTooLargeError):
            await run_pipeline(deployment, real_publisher.publisher)

        events = pull_events(pubsub_test_resources, expected_count=BUILD_RESULT_FAILURE_EVENT_COUNT)

        failed = [e for e in events if e.event_type == EventType.RUN_FAILED]
        assert len(failed) == 1
        assert failed[0].data["error_code"] == ErrorCode.PIPELINE_ERROR
        assert "byte" in failed[0].data["error_message"].lower()

        # NO run.completed event
        completed = [e for e in events if e.event_type == EventType.RUN_COMPLETED]
        assert len(completed) == 0

    async def test_publish_started_failure_aborts_pipeline(
        self,
        pubsub_test_resources: PubsubTestResources,
    ):
        """publish_run_started failure prevents any flow execution; original exception propagates."""
        topic_id = pubsub_test_resources.topic_path.split("/")[-1]
        failing_publisher = FailingStartedPublisher(
            project_id=pubsub_test_resources.project_id,
            topic_id=topic_id,
            service_type="test-service",
        )

        deployment = TwoStageDeployment()
        with pytest.raises(RuntimeError, match="publish_started injection failure"):
            await run_pipeline(deployment, failing_publisher)

        # No flows should have executed
        assert len(_flow_executions) == 0

        # publish_run_failed was attempted (may succeed or fail). Since publish_run_started
        # failed before the real publisher could send, we check for 0 or 1 events.
        try:
            events = pull_events(pubsub_test_resources, expected_count=1, timeout=3.0)
        except AssertionError:
            # No events arrived — publish_run_failed may also have failed, which is acceptable
            return

        # If we got an event, it must be run.failed (not started or completed)
        assert events[0].event_type == EventType.RUN_FAILED

    async def test_event_stream_forms_valid_state_machine(
        self,
        real_publisher: PublisherWithStore,
        pubsub_test_resources: PubsubTestResources,
    ):
        """Event stream sorted by seq forms a valid state machine for both success and failure.

        Successful: run.started -> (flow.started | progress | flow.completed)* -> run.completed
        Failed: run.started -> (flow.started | progress | flow.completed | flow.failed)* -> run.failed
        Both cases use the same topic — successful run first, then failed run.
        Events from both runs are pulled together and partitioned by run_id.
        """
        intermediate_types = {
            EventType.FLOW_STARTED,
            EventType.FLOW_COMPLETED,
            EventType.FLOW_FAILED,
            EventType.TASK_STARTED,
            EventType.TASK_COMPLETED,
            EventType.TASK_FAILED,
        }

        # --- Successful run ---
        deployment = TwoStageDeployment()
        await run_pipeline(
            deployment,
            real_publisher.publisher,
            run_id="sm-success",
        )

        success_events = pull_events(pubsub_test_resources, expected_count=TWO_FLOW_SUCCESS_EVENT_COUNT)
        assert_seq_monotonic(success_events)
        assert success_events[0].event_type == EventType.RUN_STARTED
        assert success_events[-1].event_type == EventType.RUN_COMPLETED
        for event in success_events[1:-1]:
            assert event.event_type in intermediate_types

        # --- Failed run (same topic, events accumulate in subscription) ---
        failed_deployment = FailingSecondStageDeployment()
        with pytest.raises(RuntimeError, match="deliberate test failure"):
            await run_pipeline(
                failed_deployment,
                real_publisher.publisher,
                run_id="sm-failure",
            )

        failed_events = pull_events(pubsub_test_resources, expected_count=TWO_FLOW_FAILURE_EVENT_COUNT)
        assert_seq_monotonic(failed_events)
        assert failed_events[0].event_type == EventType.RUN_STARTED
        assert failed_events[-1].event_type == EventType.RUN_FAILED
        for event in failed_events[1:-1]:
            assert event.event_type in intermediate_types
