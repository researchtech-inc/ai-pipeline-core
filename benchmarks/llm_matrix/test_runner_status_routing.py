"""Tests for benchmark probe status routing."""

import pytest

from ai_pipeline_core.llm import AIModel

from ._models import BenchmarkCase, ProbeResult, RUNNER_STATUS_TEST_MODEL
from ._probes import ProbePartialAssertion
from ._runner import PROBES, _run_probe


def _case() -> BenchmarkCase:
    model_name = RUNNER_STATUS_TEST_MODEL
    return BenchmarkCase(
        case_id=f"model:{model_name}:basic_generation",
        probe_key="basic_generation",
        scope="model",
        model=AIModel(name=model_name),
        expected="pass",
        nonce="unit",
    )


async def test_bare_assertion_routes_to_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bare assertion failures are hard benchmark failures."""

    async def _probe(case: BenchmarkCase) -> ProbeResult:
        _ = case
        raise AssertionError("hard invariant failed")

    monkeypatch.setitem(PROBES, "basic_generation", _probe)

    result = await _run_probe(_case())

    assert result.actual == "fail"
    assert result.error_category == "assertion_error"
    assert result.error_message == "hard invariant failed"


async def test_probe_partial_assertion_routes_to_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    """ProbePartialAssertion remains the explicit partial-status path."""
    case = _case()
    partial = ProbeResult(
        case_id=case.case_id,
        probe_key=case.probe_key,
        scope=case.scope,
        model_name=case.model_name,
        deployment_id=case.deployment_id,
        expected=case.expected,
        actual="partial",
        elapsed_ms=0,
        notes=("known provider limitation",),
    )

    async def _probe(probe_case: BenchmarkCase) -> ProbeResult:
        _ = probe_case
        raise ProbePartialAssertion(partial)

    monkeypatch.setitem(PROBES, "basic_generation", _probe)

    result = await _run_probe(case)

    assert result.actual == "partial"
    assert result.notes == ("known provider limitation",)
