"""Shared fixtures for deployment tests."""

# pyright: reportPrivateUsage=false

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import pytest

from ai_pipeline_core import DeploymentPlan, DeploymentResult, Document, FlowOptions, FlowStep, PipelineDeployment
from ai_pipeline_core.pipeline import PipelineFlow, PipelineTask

logger = logging.getLogger(__name__)


class InputDoc(Document):
    pass


class MiddleDoc(Document):
    pass


class OutputDoc(Document):
    pass


class _TestOptions(FlowOptions):
    value: str = "ok"


class ToMiddleTask(PipelineTask):
    @classmethod
    async def run(cls, input_docs: tuple[InputDoc, ...]) -> tuple[MiddleDoc, ...]:
        return (MiddleDoc.derive(derived_from=(input_docs[0],), name="middle.txt", content="m"),)


class ToOutputTask(PipelineTask):
    @classmethod
    async def run(cls, middle_docs: tuple[MiddleDoc, ...]) -> tuple[OutputDoc, ...]:
        return (OutputDoc.derive(derived_from=(middle_docs[0],), name="output.txt", content="o"),)


class StageOne(PipelineFlow):
    async def run(self, input_docs: tuple[InputDoc, ...], options: _TestOptions) -> tuple[MiddleDoc, ...]:
        _ = options
        return await ToMiddleTask.run(input_docs=input_docs)


class StageTwo(PipelineFlow):
    async def run(self, middle_docs: tuple[MiddleDoc, ...], options: _TestOptions) -> tuple[OutputDoc, ...]:
        _ = options
        return await ToOutputTask.run(middle_docs=middle_docs)


class _TestResult(DeploymentResult):
    output_count: int = 0


@pytest.fixture
def input_documents() -> list[Document]:
    return [InputDoc.create_root(name="input.txt", content="in", reason="deployment test input")]


# ---------------------------------------------------------------------------
# Pub/Sub integration test infrastructure
# ---------------------------------------------------------------------------

# Flow execution tracker (module-level mutable list)
_flow_executions: list[str] = []


# --- Pubsub document types ---


class PubsubInputDoc(Document):
    """Input document for pubsub tests."""


class PubsubMiddleDoc(Document):
    """Intermediate document for pubsub tests."""


class PubsubOutputDoc(Document):
    """Output document for pubsub tests."""


class PubsubFinalDoc(Document):
    """Final document for 3-flow pubsub tests."""


class PubsubResult(DeploymentResult):
    """Result type for pubsub tests."""

    doc_count: int = 0


# --- Pubsub flow classes ---


class InputToMiddleTask(PipelineTask):
    """Task: PubsubInputDoc -> PubsubMiddleDoc."""

    @classmethod
    async def run(cls, input_docs: tuple[PubsubInputDoc, ...]) -> tuple[PubsubMiddleDoc, ...]:
        return (PubsubMiddleDoc.derive(derived_from=(input_docs[0],), name="middle.json", content={"a": 1}),)


class MiddleToOutputTask(PipelineTask):
    """Task: PubsubMiddleDoc -> PubsubOutputDoc."""

    @classmethod
    async def run(cls, middle_docs: tuple[PubsubMiddleDoc, ...]) -> tuple[PubsubOutputDoc, ...]:
        return (PubsubOutputDoc.derive(derived_from=(middle_docs[0],), name="output.json", content={"b": 2}),)


class ChainOutputToFinalTask(PipelineTask):
    """Task: PubsubOutputDoc -> PubsubFinalDoc."""

    @classmethod
    async def run(cls, output_docs: tuple[PubsubOutputDoc, ...]) -> tuple[PubsubFinalDoc, ...]:
        return (PubsubFinalDoc.derive(derived_from=(output_docs[0],), name="c_out.json", content={"c": 3}),)


class DirectInputToOutputTask(PipelineTask):
    """Task: PubsubInputDoc -> PubsubOutputDoc."""

    @classmethod
    async def run(cls, input_docs: tuple[PubsubInputDoc, ...]) -> tuple[PubsubOutputDoc, ...]:
        return (PubsubOutputDoc.derive(derived_from=(input_docs[0],), name="output.json", content={"done": True}),)


class InputToMiddleFlow(PipelineFlow):
    """Flow: PubsubInputDoc -> PubsubMiddleDoc."""

    name = "input_to_middle"

    async def run(self, input_docs: tuple[PubsubInputDoc, ...], options: FlowOptions) -> tuple[PubsubMiddleDoc, ...]:
        _flow_executions.append("flow_1")
        _ = options
        return await InputToMiddleTask.run(input_docs=input_docs)


class MiddleToOutputFlow(PipelineFlow):
    """Flow: PubsubMiddleDoc -> PubsubOutputDoc."""

    name = "middle_to_output"

    async def run(self, middle_docs: tuple[PubsubMiddleDoc, ...], options: FlowOptions) -> tuple[PubsubOutputDoc, ...]:
        _flow_executions.append("flow_2")
        _ = options
        return await MiddleToOutputTask.run(middle_docs=middle_docs)


class FailingMiddleToOutputFlow(PipelineFlow):
    """Flow: PubsubMiddleDoc -> raises RuntimeError."""

    name = "failing_middle_to_output"
    retries = 0

    async def run(self, middle_docs: tuple[PubsubMiddleDoc, ...], options: FlowOptions) -> tuple[PubsubOutputDoc, ...]:
        _flow_executions.append("failing_flow_2")
        _ = (middle_docs, options)
        raise RuntimeError("deliberate test failure")


class ChainInputToMiddleFlow(PipelineFlow):
    """Three-flow chain step A: PubsubInputDoc -> PubsubMiddleDoc."""

    name = "chain_input_to_middle"

    async def run(self, input_docs: tuple[PubsubInputDoc, ...], options: FlowOptions) -> tuple[PubsubMiddleDoc, ...]:
        _flow_executions.append("flow_a")
        _ = options
        return await InputToMiddleTask.run(input_docs=input_docs)


class ChainMiddleToOutputFlow(PipelineFlow):
    """Three-flow chain step B: PubsubMiddleDoc -> PubsubOutputDoc."""

    name = "chain_middle_to_output"

    async def run(self, middle_docs: tuple[PubsubMiddleDoc, ...], options: FlowOptions) -> tuple[PubsubOutputDoc, ...]:
        _flow_executions.append("flow_b")
        _ = options
        return await MiddleToOutputTask.run(middle_docs=middle_docs)


class ChainOutputToFinalFlow(PipelineFlow):
    """Three-flow chain step C: PubsubOutputDoc -> PubsubFinalDoc."""

    name = "chain_output_to_final"

    async def run(self, output_docs: tuple[PubsubOutputDoc, ...], options: FlowOptions) -> tuple[PubsubFinalDoc, ...]:
        _flow_executions.append("flow_c")
        _ = options
        return await ChainOutputToFinalTask.run(output_docs=output_docs)


class DirectInputToOutputFlow(PipelineFlow):
    """Single-flow pipeline: PubsubInputDoc -> PubsubOutputDoc."""

    name = "direct_input_to_output"

    async def run(self, input_docs: tuple[PubsubInputDoc, ...], options: FlowOptions) -> tuple[PubsubOutputDoc, ...]:
        _flow_executions.append("single_flow")
        _ = options
        return await DirectInputToOutputTask.run(input_docs=input_docs)


# --- Pubsub deployment classes ---


class TwoStageDeployment(PipelineDeployment[FlowOptions, PubsubResult]):
    """Two-flow deployment for pubsub tests."""

    flow_retries = 0

    def build_plan(self, options: FlowOptions) -> DeploymentPlan:
        _ = options
        return DeploymentPlan(steps=(FlowStep(InputToMiddleFlow()), FlowStep(MiddleToOutputFlow())))

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> PubsubResult:
        return PubsubResult(success=True, doc_count=len(documents))


class ThreeStageDeployment(PipelineDeployment[FlowOptions, PubsubResult]):
    """Three-flow deployment for pubsub tests."""

    flow_retries = 0

    def build_plan(self, options: FlowOptions) -> DeploymentPlan:
        _ = options
        return DeploymentPlan(steps=(FlowStep(ChainInputToMiddleFlow()), FlowStep(ChainMiddleToOutputFlow()), FlowStep(ChainOutputToFinalFlow())))

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> PubsubResult:
        return PubsubResult(success=True, doc_count=len(documents))


class SingleStageDeployment(PipelineDeployment[FlowOptions, PubsubResult]):
    """Single-flow deployment for pubsub tests."""

    flow_retries = 0

    def build_plan(self, options: FlowOptions) -> DeploymentPlan:
        _ = options
        return DeploymentPlan(steps=(FlowStep(DirectInputToOutputFlow()),))

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> PubsubResult:
        return PubsubResult(success=True, doc_count=len(documents))


class FailingSecondStageDeployment(PipelineDeployment[FlowOptions, PubsubResult]):
    """Two-flow deployment where second flow raises RuntimeError."""

    flow_retries = 0

    def build_plan(self, options: FlowOptions) -> DeploymentPlan:
        _ = options
        return DeploymentPlan(steps=(FlowStep(InputToMiddleFlow()), FlowStep(FailingMiddleToOutputFlow())))

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> PubsubResult:
        return PubsubResult(success=False, error="deliberate test failure")


# --- Pubsub test data structures ---


@dataclass
class CollectedEvent:
    """A parsed event pulled from a Pub/Sub subscription."""

    event_type: str
    service_type: str
    run_id: str
    envelope: dict[str, Any]
    data: dict[str, Any]
    seq: int


@dataclass
class PubsubTestResources:
    """Topic + subscription resources for a pubsub test."""

    project_id: str
    topic_path: str
    subscription_path: str
    publisher_client: Any
    subscriber_client: Any


@dataclass
class PublisherWithStore:
    """Wraps a PubSubPublisher for test fixtures."""

    publisher: Any


# --- Helper functions ---


def make_input_doc() -> PubsubInputDoc:
    """Create a PubsubInputDoc for testing."""
    return PubsubInputDoc.create_root(name="input.json", content={"test": True}, reason="test")


async def run_pipeline(
    deployment: PipelineDeployment[Any, Any],
    publisher: Any,
    *,
    run_id: str = "test-run",
    docs: list[Document] | None = None,
    start_step: int = 1,
    end_step: int | None = None,
    database: Any = None,
) -> Any:
    """Run a deployment pipeline with the given publisher."""
    _flow_executions.clear()
    if docs is None:
        docs = [make_input_doc()]
    return await deployment.run(
        run_id,
        docs,
        FlowOptions(),
        publisher=publisher,
        start_step=start_step,
        end_step=end_step,
        database=database,
    )


def pull_events(
    resources: PubsubTestResources,
    expected_count: int,
    timeout: float = 10.0,
) -> list[CollectedEvent]:
    """Pull events from a Pub/Sub subscription and return them sorted by seq.

    Blocks until expected_count events are collected or timeout is reached.
    """
    import time

    events: list[CollectedEvent] = []
    deadline = time.monotonic() + timeout

    while len(events) < expected_count and time.monotonic() < deadline:
        remaining = max(0.5, deadline - time.monotonic())
        try:
            response = resources.subscriber_client.pull(
                subscription=resources.subscription_path,
                max_messages=expected_count - len(events),
                timeout=min(remaining, 5.0),
            )
        except OSError, TimeoutError, ValueError:
            continue

        ack_ids: list[str] = []
        for msg in response.received_messages:
            ack_ids.append(msg.ack_id)
            event_type = msg.message.attributes.get("event_type", "")
            if event_type == "run.heartbeat":
                continue
            envelope = json.loads(msg.message.data)
            data = envelope.get("data", {})
            event = CollectedEvent(
                event_type=event_type,
                service_type=msg.message.attributes.get("service_type", ""),
                run_id=msg.message.attributes.get("run_id", ""),
                envelope=envelope,
                data=data,
                seq=data.get("seq", 0),
            )
            events.append(event)

        if ack_ids:
            resources.subscriber_client.acknowledge(
                subscription=resources.subscription_path,
                ack_ids=ack_ids,
            )

    assert len(events) == expected_count, f"Expected {expected_count} events, got {len(events)}"
    events.sort(key=lambda e: e.seq)
    return events


def assert_valid_cloudevent(event: CollectedEvent) -> None:
    """Assert that an event has all required CloudEvents 1.0 fields."""
    from ai_pipeline_core.deployment._pubsub import CLOUDEVENTS_SPEC_VERSION

    envelope = event.envelope
    assert envelope["specversion"] == CLOUDEVENTS_SPEC_VERSION
    assert "id" in envelope
    assert "source" in envelope
    assert "type" in envelope
    assert "time" in envelope
    assert envelope.get("datacontenttype") == "application/json"
    assert "subject" in envelope


def assert_seq_monotonic(events: list[CollectedEvent]) -> None:
    """Assert that events have strictly increasing seq values."""
    seqs = [e.seq for e in events]
    for i in range(1, len(seqs)):
        assert seqs[i] > seqs[i - 1], f"seq not monotonic at index {i}: {seqs[i]} <= {seqs[i - 1]}"


# --- Pytest fixtures for pubsub integration tests (testcontainers) ---

from collections.abc import Generator
from uuid import uuid4

from google.api_core.exceptions import GoogleAPICallError
from google.cloud.pubsub_v1 import PublisherClient, SubscriberClient
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from ai_pipeline_core.deployment._pubsub import PubSubPublisher

EMULATOR_PORT = 8085
EMULATOR_PROJECT = "test-project"


class PubSubEmulatorContainer(DockerContainer):  # pyright: ignore[reportUntypedBaseClass]
    """GCP Pub/Sub emulator via google/cloud-sdk."""

    def __init__(self) -> None:
        super().__init__("google/cloud-sdk:emulators")
        self.with_exposed_ports(EMULATOR_PORT)
        self.with_command(f"gcloud beta emulators pubsub start --host-port=0.0.0.0:{EMULATOR_PORT}")


@pytest.fixture(scope="session")
def pubsub_emulator(require_docker) -> Generator[str]:
    """Start Pub/Sub emulator container for the test session."""
    container = PubSubEmulatorContainer().waiting_for(LogMessageWaitStrategy("Server started"))
    container.start()
    host = container.get_container_host_ip()
    port = container.get_exposed_port(EMULATOR_PORT)
    emulator_host = f"{host}:{port}"
    os.environ["PUBSUB_EMULATOR_HOST"] = emulator_host

    client = PublisherClient()
    warmup_topic = client.create_topic(name=client.topic_path(EMULATOR_PROJECT, "warmup"))
    client.delete_topic(topic=warmup_topic.name)

    yield emulator_host

    del os.environ["PUBSUB_EMULATOR_HOST"]
    container.stop()


@pytest.fixture
def pubsub_test_resources(pubsub_emulator: str) -> Generator[PubsubTestResources]:
    """Create a unique topic + subscription per test, clean up after."""
    pub_client = PublisherClient()
    sub_client = SubscriberClient()

    topic_id = f"test-events-{uuid4().hex[:8]}"
    topic_path = pub_client.topic_path(EMULATOR_PROJECT, topic_id)
    sub_id = f"test-sub-{uuid4().hex[:8]}"
    sub_path = sub_client.subscription_path(EMULATOR_PROJECT, sub_id)

    pub_client.create_topic(name=topic_path)
    sub_client.create_subscription(name=sub_path, topic=topic_path)

    yield PubsubTestResources(
        project_id=EMULATOR_PROJECT,
        topic_path=topic_path,
        subscription_path=sub_path,
        publisher_client=pub_client,
        subscriber_client=sub_client,
    )

    try:
        sub_client.delete_subscription(subscription=sub_path)
    except (OSError, GoogleAPICallError) as exc:
        logger.debug("Failed to delete subscription %s during teardown: %s", sub_path, exc)
    try:
        pub_client.delete_topic(topic=topic_path)
    except (OSError, GoogleAPICallError) as exc:
        logger.debug("Failed to delete topic %s during teardown: %s", topic_path, exc)


@pytest.fixture
def real_publisher(pubsub_test_resources: PubsubTestResources) -> PublisherWithStore:
    """Create a PubSubPublisher pointed at the emulator topic."""
    topic_id = pubsub_test_resources.topic_path.split("/")[-1]
    publisher = PubSubPublisher(
        project_id=pubsub_test_resources.project_id,
        topic_id=topic_id,
        service_type="test-service",
    )
    return PublisherWithStore(publisher=publisher)
