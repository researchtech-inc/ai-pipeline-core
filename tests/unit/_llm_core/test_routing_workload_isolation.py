"""Unit tests for LLM workload routing state isolation."""

from ai_pipeline_core._llm_core import _routing
from ai_pipeline_core.pipeline._execution_context import set_execution_context
from tests.support.helpers import RecordingSpanDatabase, make_integration_context


def test_workload_skip_isolated_between_execution_contexts() -> None:
    """Deployment demotion in one execution context must not leak to another."""
    workload_id = "phase3-workload"
    deployment_id = "deployment-phase3"
    ctx_a = make_integration_context(RecordingSpanDatabase(), run_id="phase3-a")
    ctx_b = make_integration_context(RecordingSpanDatabase(), run_id="phase3-b")

    with set_execution_context(ctx_a):
        _routing.mark_slow(workload_id, deployment_id)
        _routing.mark_slow(workload_id, deployment_id)
        assert _routing.skip_ids(workload_id) == frozenset({deployment_id})

    with set_execution_context(ctx_b):
        assert _routing.skip_ids(workload_id) == frozenset()

    with set_execution_context(ctx_a):
        assert _routing.skip_ids(workload_id) == frozenset({deployment_id})

    assert _routing.skip_ids(workload_id) == frozenset()


def test_workload_record_success_preserves_active_skip_but_resets_bad_streak() -> None:
    """Successful calls clear the bad streak without ending an active timed skip."""
    workload_id = "phase3-workload-success"
    deployment_id = "deployment-phase3-success"
    ctx = make_integration_context(RecordingSpanDatabase(), run_id="phase3-success")

    with set_execution_context(ctx):
        _routing.mark_slow(workload_id, deployment_id)
        _routing.mark_slow(workload_id, deployment_id)
        assert _routing.skip_ids(workload_id) == frozenset({deployment_id})

        _routing.record_success(workload_id, deployment_id)
        assert _routing.skip_ids(workload_id) == frozenset({deployment_id})
        assert ctx._llm_workload_state[workload_id, deployment_id][0] == 0
