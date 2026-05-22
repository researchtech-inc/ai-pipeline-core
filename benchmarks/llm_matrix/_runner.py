"""Cost-budget-aware runner and JSON sink for benchmark probes."""

import json
import os
import shutil
import subprocess
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid7

from pydantic import ValidationError

import ai_pipeline_core
from ai_pipeline_core.exceptions import LLMError, StreamWatchdogError
from ai_pipeline_core.settings import settings

from ._inventory import load_litellm_inventory
from ._models import BenchmarkCase, BenchmarkInventory, BenchmarkRun, BenchmarkRunManifest, ProbeResult
from ._probes import PROBES

BENCHMARK_OUTPUT_DIR = Path(".tmp/benchmark")
BENCHMARK_RUNS_DIR = BENCHMARK_OUTPUT_DIR / "runs"
_COST_LIMIT_ENV = "LLM_BENCHMARK_COST_LIMIT_USD"
_DEFAULT_COST_LIMIT_USD = 5.0
_HASH_DISPLAY_CHARS = 12


def new_benchmark_run_id() -> str:
    """Return a new benchmark run identifier."""
    return uuid7().hex


def reset_benchmark_output(*, output_dir: Path = BENCHMARK_OUTPUT_DIR) -> None:
    """Remove stale benchmark shard artifacts before a live run starts."""
    shards_dir = output_dir / "shards"
    if shards_dir.exists():
        shutil.rmtree(shards_dir)
    shards_dir.mkdir(parents=True, exist_ok=True)


async def run_benchmark_matrix(
    cases: Sequence[BenchmarkCase],
    *,
    inventory: BenchmarkInventory | None = None,
    output_dir: Path = BENCHMARK_OUTPUT_DIR,
    cost_budget: Any | None = None,
) -> BenchmarkRun:
    """Run benchmark cases sequentially and persist a JSON run artifact."""
    resolved_inventory = inventory or load_litellm_inventory()
    run_id = uuid7().hex
    manifest = BenchmarkRunManifest(
        run_id=run_id,
        proxy_base_url_hash=_hash_value(settings.openai_base_url or ""),
        proxy_config_path=resolved_inventory.config_path,
        proxy_config_sha256=resolved_inventory.config_sha256,
        git_sha=_git_sha(),
        framework_version=ai_pipeline_core.__version__,
    )
    run = BenchmarkRun(manifest=manifest, inventory=resolved_inventory, cases=tuple(cases), results=())
    total_cost = 0.0
    results: list[ProbeResult] = []
    cost_limit = float(os.environ.get(_COST_LIMIT_ENV, _DEFAULT_COST_LIMIT_USD))

    for case in cases:
        result = await _run_probe(case)
        if result.cost_usd is not None:
            total_cost += result.cost_usd
            if cost_budget is not None:
                cost_budget.add(result.cost_usd)
        results.append(result)
        run = _with_progress(run, results=tuple(results), total_cost=total_cost)
        _write_run(run, output_dir=output_dir)
        if total_cost > cost_limit:
            raise AssertionError(
                f"LLM benchmark cost ${total_cost:.4f} exceeded hard limit ${cost_limit:.2f}. Partial JSON was written."
            )

    finished_manifest = run.manifest.model_copy(
        update={
            "finished_at": datetime.now(UTC),
            "total_cost_usd": total_cost,
            "total_calls": sum(1 for result in results if result.actual != "skip"),
        }
    )
    run = run.model_copy(update={"manifest": finished_manifest, "results": tuple(results)})
    _write_run(run, output_dir=output_dir)
    return run


async def run_benchmark_case(
    case: BenchmarkCase,
    *,
    inventory: BenchmarkInventory,
    run_id: str,
    all_cases: Sequence[BenchmarkCase],
    output_dir: Path = BENCHMARK_OUTPUT_DIR,
    cost_budget: Any | None = None,
    worker_id: str = "master",
) -> ProbeResult:
    """Run one benchmark case and append it to that worker's shard JSON."""
    result = await _run_probe(case)
    if result.cost_usd is not None and cost_budget is not None:
        cost_budget.add(result.cost_usd)
    run = _load_or_create_shard(
        run_id=run_id, inventory=inventory, cases=all_cases, output_dir=output_dir, worker_id=worker_id
    )
    results_by_case = {existing.case_id: existing for existing in run.results}
    results_by_case[result.case_id] = result
    ordered_results = tuple(results_by_case[case.case_id] for case in all_cases if case.case_id in results_by_case)
    total_cost = sum(float(existing.cost_usd or 0.0) for existing in ordered_results)
    cost_limit = float(os.environ.get(_COST_LIMIT_ENV, _DEFAULT_COST_LIMIT_USD))
    run = _with_progress(run, results=ordered_results, total_cost=total_cost)
    _write_shard(run, output_dir=output_dir, worker_id=worker_id)
    if total_cost > cost_limit:
        raise AssertionError(
            f"LLM benchmark shard cost ${total_cost:.4f} exceeded hard limit ${cost_limit:.2f}. Shard JSON was written."
        )
    return result


def merge_benchmark_shards(
    *,
    run_id: str,
    inventory: BenchmarkInventory,
    cases: Sequence[BenchmarkCase],
    output_dir: Path = BENCHMARK_OUTPUT_DIR,
) -> BenchmarkRun | None:
    """Merge per-worker shard JSON into the final latest.json artifact."""
    shards_dir = output_dir / "shards"
    shard_paths = sorted(shards_dir.glob(f"*-{run_id}.json"))
    if not shard_paths:
        return None
    results_by_case: dict[str, ProbeResult] = {}
    for shard_path in shard_paths:
        shard = BenchmarkRun.model_validate_json(shard_path.read_text(encoding="utf-8"))
        for result in shard.results:
            results_by_case[result.case_id] = result
    ordered_results = tuple(results_by_case[case.case_id] for case in cases if case.case_id in results_by_case)
    total_cost = sum(float(result.cost_usd or 0.0) for result in ordered_results)
    manifest = BenchmarkRunManifest(
        run_id=run_id,
        finished_at=datetime.now(UTC),
        proxy_base_url_hash=_hash_value(settings.openai_base_url or ""),
        proxy_config_path=inventory.config_path,
        proxy_config_sha256=inventory.config_sha256,
        total_cost_usd=total_cost,
        total_calls=sum(1 for result in ordered_results if result.actual != "skip"),
        git_sha=_git_sha(),
        framework_version=ai_pipeline_core.__version__,
    )
    run = BenchmarkRun(manifest=manifest, inventory=inventory, cases=tuple(cases), results=ordered_results)
    _write_run(run, output_dir=output_dir)
    return run


async def _run_probe(case: BenchmarkCase) -> ProbeResult:
    if case.expected == "skip":
        return _skip(case)
    started = time.perf_counter()
    try:
        result = await PROBES[case.probe_key](case)
    except LLMError as exc:
        return _with_elapsed(_fail(case, "llm_error", exc), started)
    except (ValidationError, TimeoutError, StreamWatchdogError) as exc:  # fmt: skip
        return _with_elapsed(_fail(case, type(exc).__name__.lower(), exc), started)
    except AssertionError as exc:
        result = getattr(exc, "result", None)
        if isinstance(result, ProbeResult):
            return _with_elapsed(result, started)
        return _with_elapsed(_fail(case, "assertion_error", exc), started)
    return _with_elapsed(result, started)


def _fail(case: BenchmarkCase, category: str, exc: BaseException) -> ProbeResult:
    return ProbeResult(
        case_id=case.case_id,
        probe_key=case.probe_key,
        scope=case.scope,
        model_name=case.model_name,
        deployment_id=case.deployment_id,
        expected=case.expected,
        actual="fail",
        elapsed_ms=0,
        error_category=category,
        error_message=str(exc),
    )


def _partial(case: BenchmarkCase, note: str) -> ProbeResult:
    return ProbeResult(
        case_id=case.case_id,
        probe_key=case.probe_key,
        scope=case.scope,
        model_name=case.model_name,
        deployment_id=case.deployment_id,
        expected=case.expected,
        actual="partial",
        elapsed_ms=0,
        notes=(note,),
    )


def _skip(case: BenchmarkCase) -> ProbeResult:
    return ProbeResult(
        case_id=case.case_id,
        probe_key=case.probe_key,
        scope=case.scope,
        model_name=case.model_name,
        deployment_id=case.deployment_id,
        expected=case.expected,
        actual="skip",
        elapsed_ms=0,
        notes=("not applicable for this model/probe scope",),
    )


def _with_elapsed(result: ProbeResult, started: float) -> ProbeResult:
    return result.model_copy(update={"elapsed_ms": int((time.perf_counter() - started) * 1000)})


def _with_progress(run: BenchmarkRun, *, results: tuple[ProbeResult, ...], total_cost: float) -> BenchmarkRun:
    manifest = run.manifest.model_copy(
        update={"total_cost_usd": total_cost, "total_calls": sum(1 for result in results if result.actual != "skip")}
    )
    return run.model_copy(update={"manifest": manifest, "results": results})


def _write_run(run: BenchmarkRun, *, output_dir: Path) -> None:
    runs_dir = output_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    run_path = runs_dir / f"{run.manifest.run_id}.json"
    latest_path = output_dir / "latest.json"
    payload = json.dumps(run.model_dump(mode="json"), indent=2, sort_keys=True)
    run_path.write_text(f"{payload}\n", encoding="utf-8")
    latest_path.write_text(f"{payload}\n", encoding="utf-8")


def _load_or_create_shard(
    *,
    run_id: str,
    inventory: BenchmarkInventory,
    cases: Sequence[BenchmarkCase],
    output_dir: Path,
    worker_id: str,
) -> BenchmarkRun:
    shard_path = output_dir / "shards" / f"{worker_id}-{run_id}.json"
    if shard_path.exists():
        return BenchmarkRun.model_validate_json(shard_path.read_text(encoding="utf-8"))
    manifest = BenchmarkRunManifest(
        run_id=run_id,
        proxy_base_url_hash=_hash_value(settings.openai_base_url or ""),
        proxy_config_path=inventory.config_path,
        proxy_config_sha256=inventory.config_sha256,
        git_sha=_git_sha(),
        framework_version=ai_pipeline_core.__version__,
    )
    return BenchmarkRun(manifest=manifest, inventory=inventory, cases=tuple(cases), results=())


def _write_shard(run: BenchmarkRun, *, output_dir: Path, worker_id: str) -> None:
    shards_dir = output_dir / "shards"
    shards_dir.mkdir(parents=True, exist_ok=True)
    shard_path = shards_dir / f"{worker_id}-{run.manifest.run_id}.json"
    payload = json.dumps(run.model_dump(mode="json"), indent=2, sort_keys=True)
    shard_path.write_text(f"{payload}\n", encoding="utf-8")


def _hash_value(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()[:_HASH_DISPLAY_CHARS]


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):  # fmt: skip
        return ""
