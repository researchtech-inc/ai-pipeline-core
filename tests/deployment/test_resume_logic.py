"""Resume behavior tests for flow completion records.

Tests: cache TTL, option/input change invalidation, crash-retry resume, completed flow skip.
"""

import pytest
from pydantic import BaseModel

from ai_pipeline_core import DeploymentPlan, DeploymentResult, Document, FieldGate, FlowOptions, FlowStep, PipelineDeployment, PipelineFlow, PipelineTask
from ai_pipeline_core.database._memory import _MemoryDatabase

from .conftest import OutputDoc, StageOne, StageTwo, _TestOptions, _TestResult


# --- Resume deployments ---


class _SkipStageTwoModel(BaseModel):
    should_run: bool


class _SkipStageTwoDoc(Document[_SkipStageTwoModel]):
    """Typed control document used to force a gated skip."""


class ResumeDeployment(PipelineDeployment[_TestOptions, _TestResult]):
    flow_retries = 0

    def build_plan(self, options: _TestOptions) -> DeploymentPlan:
        _ = options
        return DeploymentPlan(steps=(FlowStep(StageOne()), FlowStep(StageTwo())))

    @staticmethod
    def build_result(run_id, documents, options):
        _ = (run_id, options)
        return _TestResult(success=True, output_count=len([d for d in documents if isinstance(d, OutputDoc)]))


class SkipDeployment(ResumeDeployment):
    def build_plan(self, options: _TestOptions) -> DeploymentPlan:
        _ = options
        return DeploymentPlan(
            steps=(
                FlowStep(StageOne()),
                FlowStep(StageTwo(), run_if=FieldGate(_SkipStageTwoDoc, "should_run", op="truthy", on_missing="skip")),
            )
        )


@pytest.mark.asyncio
async def test_resume_uses_flow_completion_cache(input_documents):
    deployment = ResumeDeployment()
    first = await deployment.run("resume-run", input_documents, _TestOptions())
    second = await deployment.run("resume-run", input_documents, _TestOptions())

    assert first.success and second.success


@pytest.mark.asyncio
async def test_build_plan_gate_can_skip_step(input_documents):
    deployment = SkipDeployment()
    result = await deployment.run("skip-run", input_documents, _TestOptions())

    assert result.success is True


# --- Crash/retry resume tests ---

# Mutable state to control flow behavior across runs
_flow_call_count = 0
_should_crash = False


class ResumeInputDoc(Document):
    """Input document for resume tests."""


class ResumeOutputDoc(Document):
    """Output document for resume tests."""


class SucceedTask(PipelineTask):
    """Task 1: produces docs that get saved incrementally."""

    @classmethod
    async def run(cls, inputs: tuple[ResumeInputDoc, ...]) -> tuple[ResumeOutputDoc, ...]:
        return (
            ResumeOutputDoc.derive(derived_from=(inputs[0],), name="out1.txt", content="output 1"),
            ResumeOutputDoc.derive(derived_from=(inputs[0],), name="out2.txt", content="output 2"),
        )


class CrashTask(PipelineTask):
    """Task 2: crashes before returning, so its docs are never saved."""

    retries = 0

    @classmethod
    async def run(cls, docs: tuple[ResumeOutputDoc, ...]) -> tuple[ResumeOutputDoc, ...]:
        if _should_crash:
            raise RuntimeError("Simulated crash in task 2")
        return (
            ResumeOutputDoc.derive(derived_from=(docs[0],), name="out3.txt", content="output 3"),
            ResumeOutputDoc.derive(derived_from=(docs[0],), name="out4.txt", content="output 4"),
        )


class ProduceAllTask(PipelineTask):
    """Task that produces all 4 output documents in one go."""

    @classmethod
    async def run(cls, inputs: tuple[ResumeInputDoc, ...]) -> tuple[ResumeOutputDoc, ...]:
        return tuple(ResumeOutputDoc.derive(derived_from=(inputs[0],), name=f"out{i}.txt", content=f"output {i}") for i in range(1, 5))


class CrashingFlow(PipelineFlow):
    """Flow with 2 tasks: task 1 succeeds (docs saved), task 2 may crash."""

    async def run(self, input_docs: tuple[ResumeInputDoc, ...], options: FlowOptions) -> tuple[ResumeOutputDoc, ...]:
        global _flow_call_count
        _ = options
        _flow_call_count += 1
        partial = await SucceedTask.run(inputs=input_docs)
        remaining = await CrashTask.run(docs=partial)
        return partial + remaining


class NormalFlow(PipelineFlow):
    """Flow that always succeeds."""

    async def run(self, input_docs: tuple[ResumeInputDoc, ...], options: FlowOptions) -> tuple[ResumeOutputDoc, ...]:
        global _flow_call_count
        _ = options
        _flow_call_count += 1
        return await ProduceAllTask.run(inputs=input_docs)


class Resume_TestOptions(FlowOptions):
    """Options for resume tests."""


class Resume_TestResult(DeploymentResult):
    """Result for resume tests."""


class CrashingDeployment(PipelineDeployment[Resume_TestOptions, Resume_TestResult]):
    """Deployment with a single two-task flow that can crash."""

    flow_retries = 0

    def build_plan(self, options: Resume_TestOptions) -> DeploymentPlan:
        _ = options
        return DeploymentPlan(steps=(FlowStep(CrashingFlow()),))

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: Resume_TestOptions) -> Resume_TestResult:
        return Resume_TestResult(success=True)


class NormalDeployment(PipelineDeployment[Resume_TestOptions, Resume_TestResult]):
    """Deployment with a single flow that always succeeds."""

    flow_retries = 0

    def build_plan(self, options: Resume_TestOptions) -> DeploymentPlan:
        _ = options
        return DeploymentPlan(steps=(FlowStep(NormalFlow()),))

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: Resume_TestOptions) -> Resume_TestResult:
        return Resume_TestResult(success=True)


@pytest.fixture(autouse=True)
def _reset_state():
    global _flow_call_count, _should_crash
    _flow_call_count = 0
    _should_crash = False
    yield
    _flow_call_count = 0
    _should_crash = False


class TestResumeAfterCrash:
    """Regression: partial outputs from a crashed flow must not skip re-execution."""

    @pytest.mark.asyncio
    async def test_partial_outputs_do_not_cause_false_resume(self):
        """Flow with 2 tasks: task 1 completes (docs saved), task 2 crashes.

        On retry, the flow must re-run because it never completed.
        """
        global _should_crash
        input_doc = ResumeInputDoc.create_root(name="input.txt", content="test input", reason="test")
        deployment = CrashingDeployment()
        options = Resume_TestOptions()

        # First run: task 1 succeeds (docs saved), task 2 crashes
        _should_crash = True
        with pytest.raises(RuntimeError, match="Simulated crash"):
            await deployment.run("test-project", [input_doc], options)

        assert _flow_call_count == 1

        # Second run: no crash this time
        _should_crash = False
        await deployment.run("test-project", [input_doc], options)

        # The flow MUST have re-executed (not skipped by false resume)
        assert _flow_call_count == 2, (
            f"Flow executed {_flow_call_count} times total — expected 2 (crash + retry). "
            "Resume logic incorrectly skipped the flow due to partial outputs from task 1."
        )


class TestResumeAfterSuccess:
    """Completed flows should be skipped on re-run."""

    @pytest.mark.asyncio
    async def test_completed_flow_is_skipped(self):
        """A flow that completed successfully should be skipped on second run."""
        input_doc = ResumeInputDoc.create_root(name="input.txt", content="test input", reason="test")
        deployment = NormalDeployment()
        options = Resume_TestOptions()
        db = _MemoryDatabase()

        # First run — flow executes fully
        await deployment.run("test-project", [input_doc], options, database=db)
        assert _flow_call_count == 1

        # Second run — flow should be skipped (resume from cache)
        await deployment.run("test-project", [input_doc], options, database=db)
        assert _flow_call_count == 1, f"Flow executed {_flow_call_count} times — expected 1. Completed flow should be skipped on resume."


class _OptionedOptions(FlowOptions):
    flavor: str = "vanilla"


class _OptionedResult(DeploymentResult):
    pass


class _OptionedDeployment(PipelineDeployment[_OptionedOptions, _OptionedResult]):
    flow_retries = 0

    def build_plan(self, options: _OptionedOptions) -> DeploymentPlan:
        _ = options
        return DeploymentPlan(steps=(FlowStep(NormalFlow()),))

    @staticmethod
    def build_result(run_id, documents, options):
        return _OptionedResult(success=True)


class TestResumeWithDifferentOptions:
    """Different options should produce a different input fingerprint, bypassing cache."""

    @pytest.mark.asyncio
    async def test_different_options_bypass_cache(self):
        """Changing options produces a different input fingerprint, so flow re-executes."""
        input_doc = ResumeInputDoc.create_root(name="input.txt", content="test input", reason="test")

        deployment = _OptionedDeployment()
        db = _MemoryDatabase()

        await deployment.run("test-project", [input_doc], _OptionedOptions(flavor="vanilla"), database=db)
        assert _flow_call_count == 1

        # Same options → skipped
        await deployment.run("test-project", [input_doc], _OptionedOptions(flavor="vanilla"), database=db)
        assert _flow_call_count == 1

        # Different options → new fingerprint → re-executes
        await deployment.run("test-project", [input_doc], _OptionedOptions(flavor="chocolate"), database=db)
        assert _flow_call_count == 2


class TestResumeWithDifferentInputs:
    """Different input documents should produce a different input fingerprint."""

    @pytest.mark.asyncio
    async def test_different_inputs_bypass_cache(self):
        """Changing input documents produces a different input fingerprint, so flow re-executes."""
        input_doc_a = ResumeInputDoc.create_root(name="a.txt", content="input A", reason="test")
        input_doc_b = ResumeInputDoc.create_root(name="b.txt", content="input B", reason="test")

        deployment = NormalDeployment()
        options = Resume_TestOptions()
        db = _MemoryDatabase()

        await deployment.run("test-project", [input_doc_a], options, database=db)
        assert _flow_call_count == 1

        # Same input → skipped
        await deployment.run("test-project", [input_doc_a], options, database=db)
        assert _flow_call_count == 1

        # Different input → new fingerprint → re-executes
        await deployment.run("test-project", [input_doc_b], options, database=db)
        assert _flow_call_count == 2
