"""Capability suite session fixtures: model input, cost budget, artifact writer."""

import os
from collections.abc import Callable, Generator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_pipeline_core.llm import AIModel

from ._budget import COST_LIMIT_ENV, DEFAULT_COST_LIMIT_USD, CapabilityCostBudget
from ._model_input import parse_models
from ._recorder import CapabilityMatrix, CapabilityResult

_ARTIFACTS_ROOT = Path("artifacts/capabilities")


@pytest.fixture(scope="session")
def models_under_test() -> tuple[AIModel, ...]:
    """Return the runtime model list parsed from CAPABILITIES_MODELS."""
    return parse_models()


@pytest.fixture(scope="session")
def capability_cost_budget() -> CapabilityCostBudget:
    """Track capability suite spend. Mid-run aborts via ``add``; teardown summary lives on the matrix fixture."""
    limit = float(os.environ.get(COST_LIMIT_ENV, DEFAULT_COST_LIMIT_USD))
    return CapabilityCostBudget(limit_usd=limit)


@pytest.fixture(scope="session", autouse=True)
def capability_matrix(capability_cost_budget: CapabilityCostBudget) -> Generator[CapabilityMatrix]:
    """Session-autouse matrix aggregator that flushes JSON + Markdown at teardown."""
    matrix = CapabilityMatrix()
    yield matrix
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    base = _ARTIFACTS_ROOT / f"matrix-{timestamp}"
    matrix.write_json(base.with_suffix(".json"))
    matrix.write_markdown(base.with_suffix(".md"))
    _print_summary(matrix, base, capability_cost_budget)


def _print_summary(matrix: CapabilityMatrix, base: Path, budget: CapabilityCostBudget) -> None:
    print()
    print(f"Capability matrix: {len(matrix.results)} cell(s)")
    print(f"  JSON: {base.with_suffix('.json')}")
    print(f"  MD:   {base.with_suffix('.md')}")
    print(f"  Cost: ${budget.total_usd:.4f} (limit ${budget.limit_usd:.2f})")
    for model, stats in matrix.per_model_summary().items():
        divergence_note = f", {stats['divergences']} divergence(s)" if stats["divergences"] else ""
        print(f"  {model}: {stats['pass']}/{stats['total']} PASS{divergence_note}")


@pytest.fixture
def record_capability(
    capability_matrix: CapabilityMatrix,
    capability_cost_budget: CapabilityCostBudget,
) -> Callable[[CapabilityResult], None]:
    """Return a callable that records a result and accumulates its cost.

    Ordering matters: the matrix is appended FIRST so the recorded cell is
    preserved when ``add`` raises after pushing the running total past the
    configured limit.
    """

    def _record(result: CapabilityResult) -> None:
        capability_matrix.record(result)
        capability_cost_budget.add(result.cost_usd)

    return _record
