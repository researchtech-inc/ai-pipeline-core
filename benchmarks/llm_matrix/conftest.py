"""Benchmark pytest fixtures and gating."""

import fnmatch
import os
from typing import Any

import pytest

from tools.benchmark.render_llm_matrix import render_files

from ._expectations import build_benchmark_cases
from ._inventory import load_litellm_inventory
from ._models import BENCHMARK_MODEL_SET, BenchmarkCase, BenchmarkInventory
from ._runner import BENCHMARK_OUTPUT_DIR, merge_benchmark_shards, new_benchmark_run_id, reset_benchmark_output

_RUN_ID_ATTR = "benchmark_run_id"
_NONCE_ATTR = "benchmark_nonce"
_BENCHMARK_NONCE_CHARS = 16
_SMOKE_CASE_TARGETS: tuple[tuple[str, str | None], ...] = (
    ("basic_generation", BENCHMARK_MODEL_SET[0]),
    ("aipl_metadata", BENCHMARK_MODEL_SET[0]),
    ("structured_basemodel", BENCHMARK_MODEL_SET[1]),
    ("structured_listof", BENCHMARK_MODEL_SET[0]),
    ("tool_single", BENCHMARK_MODEL_SET[1]),
    ("tool_plus_structured", BENCHMARK_MODEL_SET[1]),
    ("image_attach_and_split", BENCHMARK_MODEL_SET[0]),
    ("reasoning", BENCHMARK_MODEL_SET[1]),
    ("concurrency_smoke", None),
)


def pytest_configure(config: pytest.Config) -> None:
    """Initialize benchmark run identity on the controller or worker."""
    workerinput = getattr(config, "workerinput", None)
    if isinstance(workerinput, dict):
        setattr(config, _RUN_ID_ATTR, workerinput[_RUN_ID_ATTR])
        setattr(config, _NONCE_ATTR, workerinput[_NONCE_ATTR])
        return

    setattr(config, _RUN_ID_ATTR, os.environ.get("BENCHMARK_RUN_ID") or new_benchmark_run_id())
    setattr(config, _NONCE_ATTR, os.environ.get("BENCHMARK_NONCE") or new_benchmark_run_id()[:_BENCHMARK_NONCE_CHARS])
    if os.environ.get("RUN_BENCHMARK") == "1":
        reset_benchmark_output()


def pytest_configure_node(node: Any) -> None:
    """Share benchmark run identity with pytest-xdist workers."""
    node.workerinput[_RUN_ID_ATTR] = getattr(node.config, _RUN_ID_ATTR)
    node.workerinput[_NONCE_ATTR] = getattr(node.config, _NONCE_ATTR)


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Parametrize the live matrix as one pytest item per benchmark cell."""
    if "benchmark_case" not in metafunc.fixturenames:
        return
    if os.environ.get("RUN_BENCHMARK") != "1":
        metafunc.parametrize("benchmark_case", (None,), ids=("benchmark-disabled",))
        return
    inventory = load_litellm_inventory()
    nonce = _config_value(metafunc.config, _NONCE_ATTR)
    cases = _selected_benchmark_cases(inventory, nonce=nonce)
    metafunc.parametrize("benchmark_case", cases, ids=tuple(case.case_id for case in cases))


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter,
    exitstatus: int,
    config: pytest.Config,
) -> None:
    """Merge benchmark shards and render reports on the controller after workers exit."""
    _ = (terminalreporter, exitstatus)
    if _is_worker_config(config) or os.environ.get("RUN_BENCHMARK") != "1":
        return
    inventory = load_litellm_inventory()
    nonce = _config_value(config, _NONCE_ATTR)
    cases = _selected_benchmark_cases(inventory, nonce=nonce)
    run = merge_benchmark_shards(run_id=_config_value(config, _RUN_ID_ATTR), inventory=inventory, cases=cases)
    if run is not None:
        render_files(BENCHMARK_OUTPUT_DIR / "latest.json")


def _config_value(config: pytest.Config, name: str) -> str:
    value = getattr(config, name)
    if not isinstance(value, str):
        raise TypeError(f"Expected benchmark config value '{name}' to be a string.")
    return value


def _is_worker_config(config: pytest.Config) -> bool:
    return isinstance(getattr(config, "workerinput", None), dict)


def _selected_benchmark_cases(inventory: BenchmarkInventory, *, nonce: str) -> tuple[BenchmarkCase, ...]:
    cases = build_benchmark_cases(inventory, nonce=nonce)
    tier = os.environ.get("BENCHMARK_TIER")
    if tier == "smoke":
        cases = _smoke_cases(cases)
    elif tier in {None, "", "full", "nightly"}:
        # Default: RUN_BENCHMARK=1 without BENCHMARK_TIER generates the full matrix.
        # There is no separate nightly expansion in the current inventory model, so nightly is full.
        pass
    else:
        raise ValueError("BENCHMARK_TIER must be one of: smoke, full, nightly.")

    case_glob = os.environ.get("BENCHMARK_CASE_GLOB")
    if case_glob:
        cases = tuple(case for case in cases if fnmatch.fnmatch(case.case_id, case_glob))
    return cases


def _smoke_cases(cases: tuple[BenchmarkCase, ...]) -> tuple[BenchmarkCase, ...]:
    selected: list[BenchmarkCase] = []
    seen: set[str] = set()
    for probe_key, model_name in _SMOKE_CASE_TARGETS:
        case = _first_case(cases, probe_key=probe_key, model_name=model_name)
        if case is not None and case.case_id not in seen:
            selected.append(case)
            seen.add(case.case_id)
    return tuple(selected)


def _first_case(cases: tuple[BenchmarkCase, ...], *, probe_key: str, model_name: str | None) -> BenchmarkCase | None:
    for case in cases:
        if case.probe_key == probe_key and (model_name is None or case.model_name == model_name):
            return case
    for case in cases:
        if case.probe_key == probe_key:
            return case
    return None


@pytest.fixture(scope="session")
def benchmark_enabled() -> bool:
    """Return whether live benchmark execution is enabled."""
    return os.environ.get("RUN_BENCHMARK") == "1"


@pytest.fixture(scope="session")
def benchmark_run_id(request: pytest.FixtureRequest) -> str:
    """Return the shared benchmark run id."""
    return _config_value(request.config, _RUN_ID_ATTR)


@pytest.fixture(scope="session")
def benchmark_nonce(request: pytest.FixtureRequest) -> str:
    """Return a per-run nonce for deterministic cache-key isolation."""
    return _config_value(request.config, _NONCE_ATTR)


@pytest.fixture(scope="session")
def benchmark_inventory() -> BenchmarkInventory:
    """Return the parsed LiteLLM benchmark inventory."""
    return load_litellm_inventory()


@pytest.fixture(scope="session")
def benchmark_cases(benchmark_inventory: BenchmarkInventory, benchmark_nonce: str) -> tuple[BenchmarkCase, ...]:
    """Return all benchmark cases, including expected skips for reporting."""
    return _selected_benchmark_cases(benchmark_inventory, nonce=benchmark_nonce)


@pytest.fixture
def benchmark_worker_id() -> str:
    """Return the pytest-xdist worker id, or master for non-xdist runs."""
    return os.environ.get("PYTEST_XDIST_WORKER", "master")
