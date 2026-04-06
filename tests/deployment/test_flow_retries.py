"""Tests for flow retry behavior within deployments.

Covers: deployment-controlled flow retries, NonRetriableError, exponential backoff,
flow-level retry override, task retry defaults.
"""

from unittest.mock import patch

import pytest

from ai_pipeline_core import DeploymentResult, Document, FlowOptions, NonRetriableError, PipelineDeployment, PipelineFlow, PipelineTask
from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.deployment._types import _MemoryPublisher


class _RetryInputDoc(Document):
    """Input document for retry tests."""


class _RetryOutputDoc(Document):
    """Output document for retry tests."""


class _RetryResult(DeploymentResult):
    pass


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


class _CountingFailTask(PipelineTask):
    """Task that fails N times then succeeds."""

    retries = 0
    _attempt_count = 0
    _fail_count = 2

    @classmethod
    async def run(cls, input_docs: tuple[_RetryInputDoc, ...]) -> tuple[_RetryOutputDoc, ...]:
        cls._attempt_count += 1
        if cls._attempt_count <= cls._fail_count:
            raise RuntimeError(f"transient failure attempt {cls._attempt_count}")
        return (_RetryOutputDoc.derive(derived_from=input_docs, name="out.txt", content="ok"),)


class _NonRetriableTask(PipelineTask):
    retries = 0

    @classmethod
    async def run(cls, input_docs: tuple[_RetryInputDoc, ...]) -> tuple[_RetryOutputDoc, ...]:
        _ = input_docs
        raise NonRetriableError("fatal: bad config")


class _AlwaysFailTask(PipelineTask):
    retries = 0

    @classmethod
    async def run(cls, input_docs: tuple[_RetryInputDoc, ...]) -> tuple[_RetryOutputDoc, ...]:
        _ = input_docs
        raise RuntimeError("always fails")


# ---------------------------------------------------------------------------
# Flows
# ---------------------------------------------------------------------------


class _CountingFailFlow(PipelineFlow):
    """Flow that fails via task, then succeeds when task stops failing."""

    async def run(self, input_docs: tuple[_RetryInputDoc, ...], options: FlowOptions) -> tuple[_RetryOutputDoc, ...]:
        _ = options
        return await _CountingFailTask.run(input_docs=input_docs)


class _NonRetriableFlow(PipelineFlow):
    async def run(self, input_docs: tuple[_RetryInputDoc, ...], options: FlowOptions) -> tuple[_RetryOutputDoc, ...]:
        _ = (input_docs, options)
        raise NonRetriableError("flow-level non-retriable")


class _AlwaysFailFlow(PipelineFlow):
    async def run(self, input_docs: tuple[_RetryInputDoc, ...], options: FlowOptions) -> tuple[_RetryOutputDoc, ...]:
        _ = options
        return await _AlwaysFailTask.run(input_docs=input_docs)


class _ExplicitNoRetryFlow(PipelineFlow):
    """Flow that explicitly sets retries=0, overriding deployment default."""

    retries = 0

    async def run(self, input_docs: tuple[_RetryInputDoc, ...], options: FlowOptions) -> tuple[_RetryOutputDoc, ...]:
        _ = options
        return await _AlwaysFailTask.run(input_docs=input_docs)


# ---------------------------------------------------------------------------
# Deployments
# ---------------------------------------------------------------------------


class _RetryDeployment(PipelineDeployment[FlowOptions, _RetryResult]):
    flow_retries = 2
    flow_retry_delay_seconds = 0

    def build_flows(self, options: FlowOptions) -> list[PipelineFlow]:
        return [_CountingFailFlow()]

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> _RetryResult:
        return _RetryResult(success=True)


class _NonRetriableDeployment(PipelineDeployment[FlowOptions, _RetryResult]):
    flow_retries = 3
    flow_retry_delay_seconds = 0

    def build_flows(self, options: FlowOptions) -> list[PipelineFlow]:
        return [_NonRetriableFlow()]

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> _RetryResult:
        return _RetryResult(success=False, error="non-retriable")


class _ExhaustRetriesDeployment(PipelineDeployment[FlowOptions, _RetryResult]):
    flow_retries = 2
    flow_retry_delay_seconds = 0

    def build_flows(self, options: FlowOptions) -> list[PipelineFlow]:
        return [_AlwaysFailFlow()]

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> _RetryResult:
        return _RetryResult(success=False, error="exhausted")


class _FlowOverrideDeployment(PipelineDeployment[FlowOptions, _RetryResult]):
    """Deployment with flow_retries=3, but the flow explicitly sets retries=0."""

    flow_retries = 3
    flow_retry_delay_seconds = 0

    def build_flows(self, options: FlowOptions) -> list[PipelineFlow]:
        return [_ExplicitNoRetryFlow()]

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> _RetryResult:
        return _RetryResult(success=False, error="override")


class _DefaultRetryDeployment(PipelineDeployment[FlowOptions, _RetryResult]):
    """Deployment using default flow_retries=3."""

    flow_retry_delay_seconds = 0

    def build_flows(self, options: FlowOptions) -> list[PipelineFlow]:
        return [_AlwaysFailFlow()]

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> _RetryResult:
        return _RetryResult(success=False)


def _make_input() -> _RetryInputDoc:
    return _RetryInputDoc.create_root(name="in.txt", content="test", reason="retry-test")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFlowRetryWithinDeployment:
    """Verify that deployment-controlled flow retries work correctly."""

    async def test_flow_succeeds_after_retry(self) -> None:
        _CountingFailTask._attempt_count = 0
        _CountingFailTask._fail_count = 2
        result = await _RetryDeployment().run("retry-ok", [_make_input()], FlowOptions(), database=_MemoryDatabase())
        assert result.success is True
        assert _CountingFailTask._attempt_count == 3  # 2 failures + 1 success

    async def test_non_retriable_error_stops_immediately(self) -> None:
        pub = _MemoryPublisher()
        with pytest.raises(NonRetriableError, match="flow-level non-retriable"):
            await _NonRetriableDeployment().run("nr-1", [_make_input()], FlowOptions(), publisher=pub, database=_MemoryDatabase())

    async def test_exhausts_all_retries(self) -> None:
        _AlwaysFailTask._attempt_count = 0  # type: ignore[attr-defined]
        with pytest.raises(RuntimeError, match="always fails"):
            await _ExhaustRetriesDeployment().run("exhaust", [_make_input()], FlowOptions(), database=_MemoryDatabase())

    async def test_flow_explicit_retries_override_deployment(self) -> None:
        """When a flow explicitly sets retries=0, the deployment's flow_retries is ignored."""
        with pytest.raises(RuntimeError, match="always fails"):
            await _FlowOverrideDeployment().run("override", [_make_input()], FlowOptions(), database=_MemoryDatabase())

    async def test_default_deployment_flow_retries_is_none(self) -> None:
        assert _DefaultRetryDeployment.flow_retries is None


class TestTaskRetryDefaults:
    """Verify task retry defaults are None (use Settings)."""

    def test_task_default_retries(self) -> None:
        assert _CountingFailTask.retries == 0  # explicitly set
        from ai_pipeline_core.pipeline._task import PipelineTask

        assert PipelineTask.retries is None
        assert PipelineTask.retry_delay_seconds is None

    def test_settings_provides_defaults(self) -> None:
        from ai_pipeline_core.settings import Settings

        s = Settings()
        assert s.task_retries == 0
        assert s.task_retry_delay_seconds == 30
        assert s.flow_retries == 0
        assert s.flow_retry_delay_seconds == 30


class TestFlowRetryDefaults:
    """Verify flow retry defaults are None (use Settings)."""

    def test_flow_default_retries_is_none(self) -> None:
        assert _CountingFailFlow.retries is None

    def test_flow_explicit_override_preserved(self) -> None:
        assert _ExplicitNoRetryFlow.retries == 0

    def test_deployment_default_flow_retries_is_none(self) -> None:
        assert PipelineDeployment.flow_retries is None
        assert PipelineDeployment.flow_retry_delay_seconds is None


class TestNonRetriableErrorClassification:
    """Verify _classify_error unwraps NonRetriableError."""

    def test_plain_non_retriable(self) -> None:
        from ai_pipeline_core.deployment._helpers import _classify_error
        from ai_pipeline_core.deployment._types import ErrorCode

        assert _classify_error(NonRetriableError("x")) == ErrorCode.PIPELINE_ERROR

    def test_non_retriable_wrapping_llm_error(self) -> None:
        from ai_pipeline_core.deployment._helpers import _classify_error
        from ai_pipeline_core.deployment._types import ErrorCode
        from ai_pipeline_core.exceptions import LLMError

        exc = NonRetriableError("wrapped")
        exc.__cause__ = LLMError("timeout")
        assert _classify_error(exc) == ErrorCode.PROVIDER_ERROR

    def test_non_retriable_wrapping_value_error(self) -> None:
        from ai_pipeline_core.deployment._helpers import _classify_error
        from ai_pipeline_core.deployment._types import ErrorCode

        exc = NonRetriableError("bad input")
        exc.__cause__ = ValueError("nope")
        assert _classify_error(exc) == ErrorCode.INVALID_INPUT


class _BackoffTask(PipelineTask):
    retries = 3
    retry_delay_seconds = 5
    _count = 0

    @classmethod
    async def run(cls, input_docs: tuple[_RetryInputDoc, ...]) -> tuple[_RetryOutputDoc, ...]:
        _ = input_docs
        cls._count += 1
        raise RuntimeError("always fails")


class TestExponentialBackoff:
    """Verify exponential backoff timing for task retries."""

    async def test_task_exponential_backoff_delays(self) -> None:
        """Verify sleep is called with exponential delays."""
        from ai_pipeline_core.pipeline._execution_context import pipeline_test_context

        _BackoffTask._count = 0
        recorded_delays: list[float] = []

        async def mock_sleep(delay: float) -> None:
            recorded_delays.append(delay)

        with pipeline_test_context(), patch("asyncio.sleep", side_effect=mock_sleep):
            with pytest.raises(RuntimeError, match="always fails"):
                await _BackoffTask.run((_make_input(),))

        # 4 attempts, 3 sleeps between them: 5*2^0=5, 5*2^1=10, 5*2^2=20
        assert recorded_delays == [5, 10, 20]
        assert _BackoffTask._count == 4
