"""Live LLM benchmark matrix entry point."""

import os

import pytest

from ._models import BenchmarkCase, BenchmarkInventory
from ._runner import run_benchmark_case

pytestmark = pytest.mark.timeout(120)


async def test_llm_benchmark_matrix(
    benchmark_enabled: bool,
    benchmark_case: BenchmarkCase | None,
    benchmark_cases: tuple[BenchmarkCase, ...],
    benchmark_inventory: BenchmarkInventory,
    benchmark_run_id: str,
    benchmark_worker_id: str,
    cost_budget: object,
) -> None:
    """Run one live benchmark matrix cell when RUN_BENCHMARK=1."""
    if not benchmark_enabled or benchmark_case is None:
        return

    result = await run_benchmark_case(
        benchmark_case,
        inventory=benchmark_inventory,
        run_id=benchmark_run_id,
        all_cases=benchmark_cases,
        cost_budget=cost_budget,
        worker_id=benchmark_worker_id,
    )
    assert result.case_id == benchmark_case.case_id
    assert os.environ.get("RUN_BENCHMARK") == "1"
