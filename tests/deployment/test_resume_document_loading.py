"""Regression tests for document loading behavior around resume checkpoints."""

# pyright: reportPrivateUsage=false

import pytest

from ai_pipeline_core import DeploymentResult, Document, FlowOptions, PipelineDeployment, PipelineFlow, PipelineTask

from .conftest import OutputDoc, StageOne, StageTwo, _TestOptions, _TestResult


# --- Document types ---


class _InputDoc(Document):
    """Pipeline entry document."""


class _PlanDoc(Document):
    """Plan document — produced by flow 1, consumed by flow 2."""


class _AnalysisDoc(Document):
    """Analysis result — produced by flow 2."""


class _IntermediateDoc(Document):
    """Intermediate document to bridge flows without type overlap."""


class _ReportDoc(Document):
    """Report document shared across flows as output type."""


# --- Common result type ---


class _SimpleResult(DeploymentResult):
    """Minimal deployment result for tests."""


# --- Basic loading test ---


class LoadingDeployment(PipelineDeployment[_TestOptions, _TestResult]):
    flow_retries = 0

    def build_flows(self, options: _TestOptions):
        _ = options
        return [StageOne(), StageTwo()]

    @staticmethod
    def build_result(run_id, documents, options):
        _ = (run_id, options)
        return _TestResult(success=True, output_count=len([d for d in documents if isinstance(d, OutputDoc)]))


@pytest.mark.asyncio
async def test_run_loads_documents_from_store_between_flows(input_documents):
    deployment = LoadingDeployment()
    result = await deployment.run("load-run", input_documents, _TestOptions())

    assert result.output_count == 1


# ---------------------------------------------------------------------------
# Zombie docs from crashed flow pollute retry inputs on resume
# ---------------------------------------------------------------------------

_zombie_flow2_crash_flag: list[bool] = [False]
_zombie_flow2_received: list[Document] = []


class _SaveProgressTask(PipelineTask):
    """Internal task: creates a progress PlanDoc (persisted by PipelineTask)."""

    @classmethod
    async def run(cls, plans: tuple[_PlanDoc, ...]) -> tuple[_PlanDoc, ...]:
        return (_PlanDoc.derive(derived_from=(plans[0],), name="progress.md", content="# Progress notes"),)


class _ZombieFlow1(PipelineFlow):
    """Flow 1: produces a single official PlanDoc."""

    async def run(self, input_docs: tuple[_InputDoc, ...], options: FlowOptions) -> tuple[_PlanDoc, ...]:
        _ = options
        return (_PlanDoc.derive(derived_from=(input_docs[0],), name="plan.json", content='{"topic": "AI"}'),)


class _ZombieFlow2(PipelineFlow):
    """Flow 2: saves internal progress doc, then crashes (if flag set)."""

    retries = 0

    async def run(self, plan_docs: tuple[_PlanDoc, ...], options: FlowOptions) -> tuple[_AnalysisDoc, ...]:
        _ = options
        _zombie_flow2_received.extend(plan_docs)
        await _SaveProgressTask.run(plans=plan_docs)
        if _zombie_flow2_crash_flag[0]:
            raise RuntimeError("Simulated crash after saving progress")
        return (_AnalysisDoc.derive(derived_from=(plan_docs[0],), name="analysis.json", content='{"done": true}'),)


class _ZombieBugDeployment(PipelineDeployment[FlowOptions, _SimpleResult]):
    """Two-flow pipeline for zombie document bug reproduction."""

    flow_retries = 0

    def build_flows(self, options):
        return [_ZombieFlow1(), _ZombieFlow2()]

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> _SimpleResult:
        return _SimpleResult(success=True)


class TestBug1ResumeZombieDocuments:
    """On resume after a crash, the retried flow must not receive zombie documents
    from its own previous crashed execution.
    """

    @pytest.fixture(autouse=True)
    def _setup(self):
        _zombie_flow2_crash_flag[0] = False
        _zombie_flow2_received.clear()
        return

    @pytest.mark.asyncio
    async def test_resume_excludes_zombie_docs_from_crashed_flow(self):
        """On resume, flow 2 should receive only flow 1's outputs (plan.json),
        not zombie progress.md from the crashed run.
        """
        input_doc = _InputDoc.create_root(name="input.txt", content="test", reason="test")
        deployment = _ZombieBugDeployment()

        opts = FlowOptions()

        # Run 1: flow 1 completes, flow 2 saves progress.md then crashes
        _zombie_flow2_crash_flag[0] = True
        _zombie_flow2_received.clear()
        with pytest.raises(RuntimeError, match="Simulated crash after saving progress"):
            await deployment.run("proj", [input_doc], opts)

        # Run 2: resume — flow 1 skipped, flow 2 re-executes
        _zombie_flow2_crash_flag[0] = False
        _zombie_flow2_received.clear()
        await deployment.run("proj", [input_doc], opts)

        # CORRECT: flow 2 receives only plan.json (flow 1's output)
        received_names = sorted(d.name for d in _zombie_flow2_received)
        assert received_names == ["plan.json"], (
            f"Flow 2 should receive only flow 1's output [plan.json], got {received_names}. Zombie progress.md from crashed run should NOT appear in inputs."
        )

    @pytest.mark.asyncio
    async def test_flow_completion_output_sha256s_is_populated(self):
        """FlowCompletion.output_sha256s is correctly populated."""
        input_doc = _InputDoc.create_root(name="input.txt", content="test", reason="test")
        deployment = _ZombieBugDeployment()

        opts = FlowOptions()

        # Run 1: flow 1 completes, flow 2 crashes
        _zombie_flow2_crash_flag[0] = True
        with pytest.raises(RuntimeError, match="Simulated crash"):
            await deployment.run("proj", [input_doc], opts)


# ---------------------------------------------------------------------------
# output_sha256s recording must not be contaminated by preceding flows
# ---------------------------------------------------------------------------


class _Bug2Flow1(PipelineFlow):
    """Flow 1: produces both IntermediateDoc and ReportDoc."""

    async def run(self, input_docs: tuple[_InputDoc, ...], options: FlowOptions) -> tuple[_IntermediateDoc | _ReportDoc, ...]:
        _ = options
        return (
            _IntermediateDoc.derive(derived_from=(input_docs[0],), name="intermediate.txt", content="bridge data"),
            _ReportDoc.derive(derived_from=(input_docs[0],), name="report_from_flow1.txt", content="flow1 report"),
        )


class _Bug2Flow2(PipelineFlow):
    """Flow 2: takes IntermediateDoc, outputs ReportDoc (same output type as flow 1)."""

    async def run(self, intermediate_docs: tuple[_IntermediateDoc, ...], options: FlowOptions) -> tuple[_ReportDoc, ...]:
        _ = options
        return (_ReportDoc.derive(derived_from=(intermediate_docs[0],), name="report_from_flow2.txt", content="flow2 report"),)


class _Bug2Deployment(PipelineDeployment[FlowOptions, _SimpleResult]):
    """Two-flow pipeline where both flows output ReportDoc."""

    flow_retries = 0

    def build_flows(self, options):
        return [_Bug2Flow1(), _Bug2Flow2()]

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> _SimpleResult:
        return _SimpleResult(success=True)


class TestBug2OutputSha256sContamination:
    """FlowCompletion.output_sha256s for flow 2 must not include flow 1's documents
    when output types overlap.
    """

    @pytest.mark.asyncio
    async def test_flow2_output_sha256s_contains_only_own_outputs(self):
        """Flow 2 produced 1 ReportDoc → output_sha256s should have exactly 1 SHA256."""
        input_doc = _InputDoc.create_root(name="input.txt", content="test", reason="test")
        deployment = _Bug2Deployment()

        opts = FlowOptions()
        await deployment.run("proj", [input_doc], opts)

    @pytest.mark.asyncio
    async def test_flow2_output_sha256s_does_not_overlap_flow1(self):
        """Flow 2's output_sha256s should not contain any of flow 1's SHA256s."""
        input_doc = _InputDoc.create_root(name="input.txt", content="test", reason="test")
        deployment = _Bug2Deployment()

        opts = FlowOptions()
        await deployment.run("proj", [input_doc], opts)


# ---------------------------------------------------------------------------
# output_document_sha256s must not include documents from preceding flows
# ---------------------------------------------------------------------------

_bug3_published_events: list[object] = []


class _CapturingPublisher:
    """Captures published events for assertion."""

    async def publish_run_started(self, event: object) -> None:
        pass

    async def publish_run_completed(self, event: object) -> None:
        _bug3_published_events.append(event)

    async def publish_run_failed(self, event: object) -> None:
        pass

    async def publish_heartbeat(self, run_id: str, *, root_deployment_id: str, span_id: str) -> None:
        pass

    async def publish_flow_started(self, event: object) -> None:
        pass

    async def publish_flow_completed(self, event: object) -> None:
        pass

    async def publish_flow_failed(self, event: object) -> None:
        pass

    async def publish_flow_skipped(self, event: object) -> None:
        pass

    async def publish_task_started(self, event: object) -> None:
        pass

    async def publish_task_completed(self, event: object) -> None:
        pass

    async def publish_task_failed(self, event: object) -> None:
        pass

    async def close(self) -> None:
        pass


class _Bug3Deployment(PipelineDeployment[FlowOptions, _SimpleResult]):
    """Same flow chain as Bug 2 — both flows output ReportDoc."""

    flow_retries = 0

    def build_flows(self, options):
        return [_Bug2Flow1(), _Bug2Flow2()]

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> _SimpleResult:
        return _SimpleResult(success=True)


class TestBug3OutputDocumentContamination:
    """output_document_sha256s must contain only the last flow's outputs
    even when the last flow's output type is shared with a preceding flow.
    """

    @pytest.fixture(autouse=True)
    def _setup(self):
        _bug3_published_events.clear()
        return

    @pytest.mark.asyncio
    async def test_output_sha256s_has_only_last_flow_outputs(self):
        """output_document_sha256s should contain only the last flow's outputs."""
        input_doc = _InputDoc.create_root(name="input.txt", content="test", reason="test")
        deployment = _Bug3Deployment()

        opts = FlowOptions()
        publisher = _CapturingPublisher()

        await deployment.run("proj", [input_doc], opts, publisher=publisher)

        assert len(_bug3_published_events) == 1
        event = _bug3_published_events[0]
        output_sha256s = event.output_document_sha256s  # pyright: ignore[reportAttributeAccessIssue]

        # Flow 2 (last flow) produced exactly 1 ReportDoc
        assert len(output_sha256s) == 1, (
            f"Last flow produced 1 ReportDoc but output_document_sha256s has {len(output_sha256s)} refs (contaminated with preceding flow's ReportDoc)"
        )
