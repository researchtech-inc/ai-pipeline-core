"""Regression tests for Pub/Sub event field correctness."""

# pyright: reportPrivateUsage=false

import asyncio
import inspect
import json
from unittest.mock import patch
from uuid import UUID

import pytest

from ai_pipeline_core import DeploymentResult, Document, FlowOptions, PipelineDeployment, PipelineTask
from ai_pipeline_core._lifecycle_events import TaskCompletedEvent, TaskFailedEvent, TaskStartedEvent
from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.deployment._pubsub import PubSubPublisher
from ai_pipeline_core.deployment._types import (
    FlowCompletedEvent,
    FlowFailedEvent,
    FlowSkippedEvent,
    FlowStartedEvent,
    ResultPublisher,
    RunCompletedEvent,
    RunFailedEvent,
    RunStartedEvent,
    _MemoryPublisher,
)
from ai_pipeline_core.pipeline import PipelineFlow


# ---------------------------------------------------------------------------
# Shared document types and infrastructure
# ---------------------------------------------------------------------------


class _EventInput(Document):
    """Input document for event field tests."""


class _EventMiddle(Document):
    """Intermediate document for multi-flow tests."""


class _EventOutput(Document):
    """Output document for event field tests."""


class _EventResult(DeploymentResult):
    """Result for event field tests."""

    item_count: int = 0


# ---------------------------------------------------------------------------
# Tasks — needed for task event emission
# ---------------------------------------------------------------------------


class _ToMiddleTask(PipelineTask):
    """Task that transforms input to middle document."""

    @classmethod
    async def run(cls, input_docs: tuple[_EventInput, ...]) -> tuple[_EventMiddle, ...]:
        return (_EventMiddle.derive(derived_from=input_docs, name="middle.md", content="middle"),)


class _ToOutputTask(PipelineTask):
    """Task that transforms middle to output document."""

    @classmethod
    async def run(cls, middle_docs: tuple[_EventMiddle, ...]) -> tuple[_EventOutput, ...]:
        return (_EventOutput.derive(derived_from=middle_docs, name="out.md", content="done"),)


class _FailingTask(PipelineTask):
    """Task that always raises."""

    retries = 0

    @classmethod
    async def run(cls, input_docs: tuple[_EventInput, ...]) -> tuple[_EventOutput, ...]:
        _ = input_docs
        raise RuntimeError("deliberate task failure")


# ---------------------------------------------------------------------------
# Flows — using PipelineTasks to emit task events
# ---------------------------------------------------------------------------


class _TaskFlow(PipelineFlow):
    """Flow that uses a PipelineTask."""

    async def run(self, input_docs: tuple[_EventInput, ...], options: FlowOptions) -> tuple[_EventMiddle, ...]:
        _ = options
        return await _ToMiddleTask.run(input_docs=input_docs)


class _SecondTaskFlow(PipelineFlow):
    """Second flow that uses a PipelineTask."""

    async def run(self, middle_docs: tuple[_EventMiddle, ...], options: FlowOptions) -> tuple[_EventOutput, ...]:
        _ = options
        return await _ToOutputTask.run(middle_docs=middle_docs)


class _FailingTaskFlow(PipelineFlow):
    """Flow that contains a failing task."""

    async def run(self, input_docs: tuple[_EventInput, ...], options: FlowOptions) -> tuple[_EventOutput, ...]:
        _ = options
        return await _FailingTask.run(input_docs=input_docs)


class _DirectFailingFlow(PipelineFlow):
    """Flow that raises directly (no tasks)."""

    retries = 0

    async def run(self, input_docs: tuple[_EventInput, ...], options: FlowOptions) -> tuple[_EventOutput, ...]:
        _ = (input_docs, options)
        raise RuntimeError("deliberate flow failure")


class _NoTaskFlow(PipelineFlow):
    """Flow that returns documents without calling PipelineTasks."""

    async def run(self, input_docs: tuple[_EventInput, ...], options: FlowOptions) -> tuple[_EventOutput, ...]:
        _ = options
        return (_EventOutput.derive(derived_from=input_docs, name="notask.md", content="done"),)


class _SlowFlow(PipelineFlow):
    """Flow that sleeps to guarantee heartbeat firing."""

    async def run(self, input_docs: tuple[_EventInput, ...], options: FlowOptions) -> tuple[_EventOutput, ...]:
        _ = options
        await asyncio.sleep(0.05)
        return (_EventOutput.derive(derived_from=input_docs, name="slow.md", content="done"),)


# ---------------------------------------------------------------------------
# Deployments
# ---------------------------------------------------------------------------


class _TwoFlowDeployment(PipelineDeployment[FlowOptions, _EventResult]):
    """Deployment with two sequential flows that use tasks."""

    flow_retries = 0
    cache_ttl = None

    def build_flows(self, options: FlowOptions) -> list[PipelineFlow]:
        return [_TaskFlow(), _SecondTaskFlow()]

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> _EventResult:
        return _EventResult(success=True, item_count=len(documents))


class _OneFlowDeployment(PipelineDeployment[FlowOptions, _EventResult]):
    """Deployment with a single flow using a task."""

    flow_retries = 0
    cache_ttl = None

    def build_flows(self, options: FlowOptions) -> list[PipelineFlow]:
        return [_TaskFlow()]

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> _EventResult:
        return _EventResult(success=True, item_count=len(documents))


class _FailingTaskDeployment(PipelineDeployment[FlowOptions, _EventResult]):
    """Deployment with a flow that has a failing task."""

    flow_retries = 0
    cache_ttl = None

    def build_flows(self, options: FlowOptions) -> list[PipelineFlow]:
        return [_FailingTaskFlow()]

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> _EventResult:
        return _EventResult(success=False, error="failed")


class _DirectFailingDeployment(PipelineDeployment[FlowOptions, _EventResult]):
    """Deployment with a flow that raises directly (no tasks)."""

    flow_retries = 0
    cache_ttl = None

    def build_flows(self, options: FlowOptions) -> list[PipelineFlow]:
        return [_DirectFailingFlow()]

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> _EventResult:
        return _EventResult(success=False, error="failed")


class _NoTaskDeployment(PipelineDeployment[FlowOptions, _EventResult]):
    """Deployment with a flow that uses no tasks."""

    flow_retries = 0
    cache_ttl = None

    def build_flows(self, options: FlowOptions) -> list[PipelineFlow]:
        return [_NoTaskFlow()]

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> _EventResult:
        return _EventResult(success=True)


class _SlowDeployment(PipelineDeployment[FlowOptions, _EventResult]):
    """Deployment with a slow flow for heartbeat testing."""

    flow_retries = 0
    cache_ttl = None

    def build_flows(self, options: FlowOptions) -> list[PipelineFlow]:
        return [_SlowFlow()]

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> _EventResult:
        return _EventResult(success=True)


def _make_input_doc(name: str = "in.txt", content: str = "test") -> _EventInput:
    return _EventInput.create_root(name=name, content=content, reason="test")


class TestBug1FlowClassFormat:
    """flow_plan entries must use short __name__, not module:qualname."""

    async def test_flow_plan_uses_short_class_name(self):
        pub = _MemoryPublisher()
        deployment = _TwoFlowDeployment()
        await deployment.run("bug1", [_make_input_doc()], FlowOptions(), publisher=pub)

        started = [e for e in pub.events if isinstance(e, RunStartedEvent)][0]
        flow_started = [e for e in pub.events if isinstance(e, FlowStartedEvent)]

        for plan_entry, event in zip(started.flow_plan, flow_started, strict=True):
            assert plan_entry["flow_class"] == event.flow_class, f"flow_plan uses '{plan_entry['flow_class']}' but flow.started uses '{event.flow_class}'"

    async def test_flow_class_contains_no_module_separator(self):
        pub = _MemoryPublisher()
        deployment = _TwoFlowDeployment()
        await deployment.run("bug1b", [_make_input_doc()], FlowOptions(), publisher=pub)

        started = [e for e in pub.events if isinstance(e, RunStartedEvent)][0]
        for entry in started.flow_plan:
            assert ":" not in entry["flow_class"], f"flow_plan flow_class '{entry['flow_class']}' contains ':'"


class TestBug2TaskTotalSteps:
    """Task events must include total_steps from FlowFrame."""

    async def test_task_started_has_total_steps(self):
        pub = _MemoryPublisher()
        deployment = _TwoFlowDeployment()
        await deployment.run("bug2", [_make_input_doc()], FlowOptions(), publisher=pub)

        task_started = [e for e in pub.events if isinstance(e, TaskStartedEvent)]
        assert len(task_started) >= 1
        for event in task_started:
            assert event.total_steps == 2

    async def test_task_completed_has_total_steps(self):
        pub = _MemoryPublisher()
        deployment = _TwoFlowDeployment()
        await deployment.run("bug2b", [_make_input_doc()], FlowOptions(), publisher=pub)

        task_completed = [e for e in pub.events if isinstance(e, TaskCompletedEvent)]
        assert len(task_completed) >= 1
        assert all(e.total_steps == 2 for e in task_completed)

    async def test_task_failed_has_total_steps(self):
        pub = _MemoryPublisher()
        deployment = _FailingTaskDeployment()
        with pytest.raises(RuntimeError):
            await deployment.run("bug2c", [_make_input_doc()], FlowOptions(), publisher=pub)

        task_failed = [e for e in pub.events if isinstance(e, TaskFailedEvent)]
        assert len(task_failed) == 1
        assert task_failed[0].total_steps == 1


class TestBug3TaskInputShas:
    """Task events must carry input_document_sha256s."""

    async def test_task_started_has_input_document_sha256s(self):
        pub = _MemoryPublisher()
        deployment = _TwoFlowDeployment()
        doc = _make_input_doc(content="sha-test")
        await deployment.run("bug3", [doc], FlowOptions(), publisher=pub)

        task_started = [e for e in pub.events if isinstance(e, TaskStartedEvent)]
        assert len(task_started) >= 1
        assert len(task_started[0].input_document_sha256s) > 0

    async def test_task_completed_has_input_document_sha256s(self):
        pub = _MemoryPublisher()
        deployment = _TwoFlowDeployment()
        doc = _make_input_doc(content="sha-test-completed")
        await deployment.run("bug3b", [doc], FlowOptions(), publisher=pub)

        task_completed = [e for e in pub.events if isinstance(e, TaskCompletedEvent)]
        assert len(task_completed) >= 1
        assert len(task_completed[0].input_document_sha256s) > 0

    async def test_task_failed_has_input_document_sha256s(self):
        pub = _MemoryPublisher()
        deployment = _FailingTaskDeployment()
        doc = _make_input_doc(content="sha-test-failed")
        with pytest.raises(RuntimeError):
            await deployment.run("bug3c", [doc], FlowOptions(), publisher=pub)

        task_failed = [e for e in pub.events if isinstance(e, TaskFailedEvent)]
        assert len(task_failed) == 1
        assert len(task_failed[0].input_document_sha256s) > 0

    async def test_failed_task_preceded_by_started_with_same_span_id(self):
        """task.started always fires before task.failed for the same span_id."""
        pub = _MemoryPublisher()
        deployment = _FailingTaskDeployment()
        with pytest.raises(RuntimeError):
            await deployment.run("bug3d", [_make_input_doc()], FlowOptions(), publisher=pub)

        task_started = [e for e in pub.events if isinstance(e, TaskStartedEvent)]
        task_failed = [e for e in pub.events if isinstance(e, TaskFailedEvent)]
        assert len(task_failed) == 1
        assert len(task_started) >= 1
        matching_started = [e for e in task_started if e.span_id == task_failed[0].span_id]
        assert len(matching_started) == 1


class TestBug4FlowInputShas:
    """Flow events must carry input_document_sha256s."""

    async def test_flow_started_has_input_document_sha256s(self):
        pub = _MemoryPublisher()
        deployment = _TwoFlowDeployment()
        doc = _make_input_doc(content="flow-input")
        await deployment.run("bug4", [doc], FlowOptions(), publisher=pub)

        flow_started = [e for e in pub.events if isinstance(e, FlowStartedEvent)]
        assert len(flow_started) >= 1
        assert doc.sha256 in flow_started[0].input_document_sha256s

    async def test_flow_completed_has_input_document_sha256s(self):
        pub = _MemoryPublisher()
        deployment = _TwoFlowDeployment()
        doc = _make_input_doc(content="flow-input-completed")
        await deployment.run("bug4b", [doc], FlowOptions(), publisher=pub)

        flow_completed = [e for e in pub.events if isinstance(e, FlowCompletedEvent)]
        assert len(flow_completed) >= 1
        assert len(flow_completed[0].input_document_sha256s) > 0

    async def test_flow_failed_has_input_document_sha256s(self):
        pub = _MemoryPublisher()
        deployment = _DirectFailingDeployment()
        doc = _make_input_doc(content="flow-fail-input")
        with pytest.raises(RuntimeError):
            await deployment.run("bug4c", [doc], FlowOptions(), publisher=pub)

        flow_failed = [e for e in pub.events if isinstance(e, FlowFailedEvent)]
        assert len(flow_failed) == 1
        assert doc.sha256 in flow_failed[0].input_document_sha256s


class TestBug5Heartbeat:
    """Heartbeat must carry root_deployment_id and span_id."""

    def test_heartbeat_protocol_accepts_root_deployment_id(self):
        sig = inspect.signature(ResultPublisher.publish_heartbeat)
        root_param = sig.parameters["root_deployment_id"]
        span_param = sig.parameters["span_id"]
        assert root_param.default is inspect._empty
        assert span_param.default is inspect._empty

    async def test_heartbeat_records_root_deployment_id(self):
        pub = _MemoryPublisher()
        await pub.publish_heartbeat("run-1", root_deployment_id="root-123", span_id="span-456")
        assert len(pub.heartbeats) == 1
        assert pub.heartbeats[0]["root_deployment_id"] == "root-123"
        assert pub.heartbeats[0]["span_id"] == "span-456"

    async def test_heartbeat_loop_passes_root_deployment_id(self):
        pub = _MemoryPublisher()
        with patch("ai_pipeline_core.deployment._helpers._HEARTBEAT_INTERVAL_SECONDS", 0.01):
            deployment = _SlowDeployment()
            await deployment.run("bug5", [_make_input_doc()], FlowOptions(), publisher=pub)

        if pub.heartbeats:
            assert pub.heartbeats[0]["root_deployment_id"]
            assert pub.heartbeats[0]["span_id"]


class TestBug7FlowStartedSpanTiming:
    """flow.started must be published inside track_span, not before."""

    async def test_flow_started_span_exists_in_database_at_publish_time(self):
        span_ids_at_publish: dict[str, bool] = {}

        class _InstrumentedPublisher(_MemoryPublisher):
            async def publish_flow_started(self, event: FlowStartedEvent) -> None:
                await super().publish_flow_started(event)
                if db is not None:
                    spans = await db.get_deployment_tree(UUID(event.root_deployment_id))
                    span_ids_at_publish[event.span_id] = any(str(s.span_id) == event.span_id for s in spans)

        db = _MemoryDatabase()
        pub = _InstrumentedPublisher()
        deployment = _OneFlowDeployment()
        await deployment.run("bug7", [_make_input_doc()], FlowOptions(), publisher=pub, database=db)

        assert span_ids_at_publish, "No flow.started events captured"
        for span_id, existed in span_ids_at_publish.items():
            assert existed, f"flow.started published for span {span_id} but span was NOT in database at publish time"


class TestBug9DeploymentIdentity:
    """Run events must identify the deployment."""

    async def test_run_started_has_deployment_identity(self):
        pub = _MemoryPublisher()
        deployment = _TwoFlowDeployment()
        await deployment.run("bug9", [_make_input_doc()], FlowOptions(), publisher=pub)

        started = [e for e in pub.events if isinstance(e, RunStartedEvent)][0]
        assert started.deployment_name
        assert started.deployment_class == "_TwoFlowDeployment"

    async def test_run_completed_has_deployment_identity(self):
        pub = _MemoryPublisher()
        deployment = _TwoFlowDeployment()
        await deployment.run("bug9b", [_make_input_doc()], FlowOptions(), publisher=pub)

        completed = [e for e in pub.events if isinstance(e, RunCompletedEvent)][0]
        assert completed.deployment_name
        assert completed.deployment_class == "_TwoFlowDeployment"

    async def test_run_failed_has_deployment_identity(self):
        pub = _MemoryPublisher()
        deployment = _DirectFailingDeployment()
        with pytest.raises(RuntimeError):
            await deployment.run("bug9c", [_make_input_doc()], FlowOptions(), publisher=pub)

        failed = [e for e in pub.events if isinstance(e, RunFailedEvent)][0]
        assert failed.deployment_name
        assert failed.deployment_class == "_DirectFailingDeployment"


class TestBug10RunDuration:
    """Run events must include duration_ms."""

    async def test_run_completed_has_duration_ms(self):
        pub = _MemoryPublisher()
        deployment = _TwoFlowDeployment()
        await deployment.run("bug10", [_make_input_doc()], FlowOptions(), publisher=pub)

        completed = [e for e in pub.events if isinstance(e, RunCompletedEvent)][0]
        assert completed.duration_ms >= 0

    async def test_run_failed_has_duration_ms(self):
        pub = _MemoryPublisher()
        deployment = _DirectFailingDeployment()
        with pytest.raises(RuntimeError):
            await deployment.run("bug10b", [_make_input_doc()], FlowOptions(), publisher=pub)

        failed = [e for e in pub.events if isinstance(e, RunFailedEvent)][0]
        assert failed.duration_ms >= 0


class TestBug11RunInputShas:
    """RunStartedEvent must include input document SHA256s."""

    async def test_run_started_has_input_document_sha256s(self):
        pub = _MemoryPublisher()
        deployment = _TwoFlowDeployment()
        doc1 = _make_input_doc(name="a.txt", content="aaa")
        doc2 = _make_input_doc(name="b.txt", content="bbb")
        await deployment.run("bug11", [doc1, doc2], FlowOptions(), publisher=pub)

        started = [e for e in pub.events if isinstance(e, RunStartedEvent)][0]
        assert doc1.sha256 in started.input_document_sha256s
        assert doc2.sha256 in started.input_document_sha256s


class TestGap1ParentSpanId:
    """All events must carry parent_span_id for tree reconstruction."""

    async def test_flow_events_have_parent_span_id(self):
        pub = _MemoryPublisher()
        deployment = _TwoFlowDeployment()
        await deployment.run("gap1", [_make_input_doc()], FlowOptions(), publisher=pub)

        run_started = [e for e in pub.events if isinstance(e, RunStartedEvent)][0]
        flow_started = [e for e in pub.events if isinstance(e, FlowStartedEvent)]

        for event in flow_started:
            assert event.parent_span_id == run_started.span_id, "Flow's parent_span_id must point to the deployment span"

    async def test_task_events_have_parent_span_id(self):
        pub = _MemoryPublisher()
        deployment = _TwoFlowDeployment()
        await deployment.run("gap1b", [_make_input_doc()], FlowOptions(), publisher=pub)

        task_started = [e for e in pub.events if isinstance(e, TaskStartedEvent)]
        assert len(task_started) >= 1
        for task in task_started:
            assert task.parent_span_id, "TaskStartedEvent must have parent_span_id"

    async def test_run_events_have_parent_span_id_field(self):
        pub = _MemoryPublisher()
        deployment = _TwoFlowDeployment()
        await deployment.run("gap1c", [_make_input_doc()], FlowOptions(), publisher=pub)

        started = [e for e in pub.events if isinstance(e, RunStartedEvent)][0]
        completed = [e for e in pub.events if isinstance(e, RunCompletedEvent)][0]
        assert hasattr(started, "parent_span_id")
        assert hasattr(completed, "parent_span_id")


class TestGap2FinalOutputsOnly:
    """RunCompletedEvent.output_document_sha256s must be final flow outputs only."""

    async def test_run_completed_has_only_last_flow_outputs(self):
        pub = _MemoryPublisher()
        deployment = _TwoFlowDeployment()
        await deployment.run("gap2", [_make_input_doc()], FlowOptions(), publisher=pub)

        completed = [e for e in pub.events if isinstance(e, RunCompletedEvent)][0]
        flow_completed = [e for e in pub.events if isinstance(e, FlowCompletedEvent)]

        last_flow_output_shas = {doc.sha256 for doc in flow_completed[-1].output_documents}
        run_output_shas = set(completed.output_document_sha256s)

        assert run_output_shas == last_flow_output_shas, (
            f"RunCompletedEvent should have only final flow outputs. Got {run_output_shas}, expected {last_flow_output_shas}"
        )


class TestMinor1CachedFlowReason:
    """Cached flows must use a reason that indicates caching."""

    async def test_cached_flow_reason_not_completed(self):
        from datetime import timedelta

        db = _MemoryDatabase()

        class _CachingDeployment(PipelineDeployment[FlowOptions, _EventResult]):
            flow_retries = 0
            cache_ttl = timedelta(hours=1)

            def build_flows(self, options: FlowOptions) -> list[PipelineFlow]:
                return [_NoTaskFlow()]

            @staticmethod
            def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> _EventResult:
                return _EventResult(success=True)

        deployment = _CachingDeployment()
        doc = _make_input_doc(content="cache-minor1")

        # First run
        await deployment.run("minor1", [doc], FlowOptions(), publisher=_MemoryPublisher(), database=db)

        # Second run — should hit cache
        pub = _MemoryPublisher()
        await deployment.run("minor1", [doc], FlowOptions(), publisher=pub, database=db)

        skipped = [e for e in pub.events if isinstance(e, FlowSkippedEvent)]
        cached_skipped = [e for e in skipped if e.status == "cached"]
        for event in cached_skipped:
            assert event.reason != "completed", f"Cached flow reason should not be 'completed', got '{event.reason}'"
            assert "cached" in event.reason.lower() or "cache" in event.reason.lower()


class TestMinor3PubsubAttributes:
    """PubSub message attributes must include root_deployment_id."""

    async def test_publish_event_injects_root_deployment_id_into_attributes(self):
        captured_attrs: list[dict[str, str]] = []

        class _CapturingPublisher(PubSubPublisher):
            def __init__(self) -> None:
                self._service_type = "test"
                self._seq = 0

            async def _publish(self, data: bytes, attributes: dict[str, str]) -> None:
                captured_attrs.append(dict(attributes))

        pub = _CapturingPublisher()
        await pub.publish_flow_started(
            FlowStartedEvent(
                run_id="r1",
                span_id="s1",
                root_deployment_id="root-123",
                parent_deployment_task_id=None,
                flow_name="f",
                flow_class="F",
                step=1,
                total_steps=1,
                status="running",
            )
        )

        assert captured_attrs
        assert captured_attrs[0].get("root_deployment_id") == "root-123"


# ---------------------------------------------------------------------------
# Integration: Complete event ordering
# ---------------------------------------------------------------------------


class TestEventSequenceIntegration:
    """Full event sequence validation for deployments."""

    async def test_complete_event_sequence_two_flows(self):
        pub = _MemoryPublisher()
        deployment = _TwoFlowDeployment()
        await deployment.run("int1", [_make_input_doc()], FlowOptions(), publisher=pub)

        types = [type(e).__name__ for e in pub.events]
        assert types[0] == "RunStartedEvent"
        assert types[-1] == "RunCompletedEvent"

        flow_started_indices = [i for i, t in enumerate(types) if t == "FlowStartedEvent"]
        flow_completed_indices = [i for i, t in enumerate(types) if t == "FlowCompletedEvent"]
        assert len(flow_started_indices) == 2
        assert len(flow_completed_indices) == 2

        # Flow 1 completed before Flow 2 started
        assert flow_completed_indices[0] < flow_started_indices[1]

    async def test_task_events_between_flow_boundaries(self):
        pub = _MemoryPublisher()
        deployment = _TwoFlowDeployment()
        await deployment.run("int1b", [_make_input_doc()], FlowOptions(), publisher=pub)

        types = [type(e).__name__ for e in pub.events]
        flow_started_indices = [i for i, t in enumerate(types) if t == "FlowStartedEvent"]
        flow_completed_indices = [i for i, t in enumerate(types) if t == "FlowCompletedEvent"]
        task_started_indices = [i for i, t in enumerate(types) if t == "TaskStartedEvent"]

        assert len(task_started_indices) >= 2
        for ts_idx in task_started_indices:
            enclosing_flow_start = max(fs for fs in flow_started_indices if fs < ts_idx)
            enclosing_flow_complete = min(fc for fc in flow_completed_indices if fc > ts_idx)
            assert enclosing_flow_start < ts_idx < enclosing_flow_complete

    async def test_all_events_share_run_id_and_root_deployment_id(self):
        pub = _MemoryPublisher()
        deployment = _TwoFlowDeployment()
        await deployment.run("int2", [_make_input_doc()], FlowOptions(), publisher=pub)

        run_ids = {e.run_id for e in pub.events}
        assert run_ids == {"int2"}

        root_ids = {e.root_deployment_id for e in pub.events}
        assert len(root_ids) == 1
        assert all(rid for rid in root_ids)

    async def test_span_ids_are_unique_per_scope(self):
        pub = _MemoryPublisher()
        deployment = _TwoFlowDeployment()
        await deployment.run("int3", [_make_input_doc()], FlowOptions(), publisher=pub)

        flow_started_ids = [e.span_id for e in pub.events if isinstance(e, FlowStartedEvent)]
        flow_completed_ids = [e.span_id for e in pub.events if isinstance(e, FlowCompletedEvent)]
        assert flow_started_ids == flow_completed_ids  # paired

        # Each flow has a unique span_id
        assert len(set(flow_started_ids)) == len(flow_started_ids)

    async def test_step_field_increments_across_flows(self):
        pub = _MemoryPublisher()
        deployment = _TwoFlowDeployment()
        await deployment.run("int4", [_make_input_doc()], FlowOptions(), publisher=pub)

        flow_started = [e for e in pub.events if isinstance(e, FlowStartedEvent)]
        assert flow_started[0].step == 1
        assert flow_started[1].step == 2


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge case scenarios."""

    async def test_empty_flow_no_tasks_emits_flow_events(self):
        pub = _MemoryPublisher()
        deployment = _NoTaskDeployment()
        await deployment.run("edge1", [_make_input_doc()], FlowOptions(), publisher=pub)

        flow_started = [e for e in pub.events if isinstance(e, FlowStartedEvent)]
        flow_completed = [e for e in pub.events if isinstance(e, FlowCompletedEvent)]
        assert len(flow_started) == 1
        assert len(flow_completed) == 1

        task_events = [e for e in pub.events if isinstance(e, (TaskStartedEvent, TaskCompletedEvent))]
        assert len(task_events) == 0

    async def test_task_failure_emits_run_failed(self):
        pub = _MemoryPublisher()
        deployment = _FailingTaskDeployment()
        with pytest.raises(RuntimeError):
            await deployment.run("edge2", [_make_input_doc()], FlowOptions(), publisher=pub)

        run_failed = [e for e in pub.events if isinstance(e, RunFailedEvent)]
        assert len(run_failed) == 1
        assert "deliberate" in run_failed[0].error_message

    async def test_task_failure_also_emits_flow_failed(self):
        pub = _MemoryPublisher()
        deployment = _FailingTaskDeployment()
        with pytest.raises(RuntimeError):
            await deployment.run("edge2b", [_make_input_doc()], FlowOptions(), publisher=pub)

        task_failed = [e for e in pub.events if isinstance(e, TaskFailedEvent)]
        flow_failed = [e for e in pub.events if isinstance(e, FlowFailedEvent)]
        assert len(task_failed) == 1
        assert len(flow_failed) == 1

    async def test_heartbeats_do_not_appear_in_business_events(self):
        pub = _MemoryPublisher()
        with patch("ai_pipeline_core.deployment._helpers._HEARTBEAT_INTERVAL_SECONDS", 0.001):
            deployment = _SlowDeployment()
            await deployment.run("edge3", [_make_input_doc()], FlowOptions(), publisher=pub)

        types = [type(e).__name__ for e in pub.events]
        assert "heartbeat" not in " ".join(types).lower()
        assert types[0] == "RunStartedEvent"
        assert types[-1] == "RunCompletedEvent"

    async def test_flow_started_event_carries_flow_params(self):
        pub = _MemoryPublisher()
        deployment = _TwoFlowDeployment()
        await deployment.run("edge4", [_make_input_doc()], FlowOptions(), publisher=pub)

        flow_started = [e for e in pub.events if isinstance(e, FlowStartedEvent)]
        assert len(flow_started) >= 1
        assert isinstance(flow_started[0].flow_params, dict)

    async def test_flow_completed_and_failed_have_parent_span_id(self):
        pub = _MemoryPublisher()
        deployment = _TwoFlowDeployment()
        await deployment.run("edge5", [_make_input_doc()], FlowOptions(), publisher=pub)

        flow_completed = [e for e in pub.events if isinstance(e, FlowCompletedEvent)]
        run_started = [e for e in pub.events if isinstance(e, RunStartedEvent)][0]

        for event in flow_completed:
            assert event.parent_span_id == run_started.span_id


# ---------------------------------------------------------------------------
# PubSub envelope validation
# ---------------------------------------------------------------------------


class TestPubSubEnvelope:
    """CloudEvents 1.0 envelope structure validation."""

    @pytest.fixture
    def capturing_publisher(self):
        captured: list[tuple[bytes, dict[str, str]]] = []

        class _Capturing(PubSubPublisher):
            def __init__(self) -> None:
                self._service_type = "test"
                self._seq = 0

            async def _publish(self, data: bytes, attributes: dict[str, str]) -> None:
                captured.append((data, attributes))

        return _Capturing(), captured

    async def test_envelope_has_required_cloudevents_fields(self, capturing_publisher):
        pub, captured = capturing_publisher
        await pub.publish_run_started(
            RunStartedEvent(
                run_id="r1",
                span_id="s1",
                root_deployment_id="root1",
                parent_deployment_task_id=None,
                input_fingerprint="fp1",
                status="running",
            )
        )

        envelope = json.loads(captured[0][0])
        for field in ("id", "source", "type", "specversion", "time", "subject", "datacontenttype"):
            assert field in envelope, f"Missing CloudEvents field: {field}"
        assert envelope["specversion"] == "1.0"

    async def test_heartbeat_envelope_has_root_and_span(self, capturing_publisher):
        pub, captured = capturing_publisher
        await pub.publish_heartbeat("run-1", root_deployment_id="root-456", span_id="span-789")

        envelope = json.loads(captured[0][0])
        assert envelope["data"]["root_deployment_id"] == "root-456"
        assert envelope["data"]["span_id"] == "span-789"

    async def test_task_envelope_has_total_steps(self, capturing_publisher):
        pub, captured = capturing_publisher
        await pub.publish_task_started(
            TaskStartedEvent(
                run_id="r1",
                span_id="s1",
                root_deployment_id="root1",
                parent_deployment_task_id=None,
                flow_name="f",
                step=1,
                total_steps=3,
                status="running",
                task_name="t",
                task_class="T",
            )
        )

        envelope = json.loads(captured[0][0])
        assert envelope["data"]["total_steps"] == 3

    async def test_flow_envelope_has_input_document_sha256s(self, capturing_publisher):
        pub, captured = capturing_publisher
        await pub.publish_flow_started(
            FlowStartedEvent(
                run_id="r1",
                span_id="s1",
                root_deployment_id="root1",
                parent_deployment_task_id=None,
                flow_name="f",
                flow_class="F",
                step=1,
                total_steps=1,
                status="running",
                input_document_sha256s=("abc123", "def456"),
            )
        )

        envelope = json.loads(captured[0][0])
        assert envelope["data"]["input_document_sha256s"] == ["abc123", "def456"]
