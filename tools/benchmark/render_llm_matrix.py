"""Render LLM benchmark JSON artifacts as Markdown and summary JSON."""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from benchmarks.llm_matrix import CANONICAL_PROBES, BenchmarkRun, ProbeResult

_SYMBOLS = {
    "pass": "PASS",
    "partial": "PARTIAL",
    "fail": "FAIL",
    "skip": "SKIP",
}


def render_files(input_path: Path) -> tuple[Path, Path, Path]:
    """Render Markdown, investigation, and summary files for one benchmark JSON artifact."""
    run = BenchmarkRun.model_validate_json(input_path.read_text(encoding="utf-8"))
    output_dir = input_path.parent if input_path.name == "latest.json" else input_path.parents[1]
    output_dir.mkdir(parents=True, exist_ok=True)
    matrix_path = output_dir / "latest.md"
    investigation_path = output_dir / "needs_investigation.md"
    summary_path = output_dir / "summary.json"
    matrix_text = render_matrix(run)
    investigation_text = render_needs_investigation(run)
    summary = render_summary(run)
    matrix_path.write_text(matrix_text, encoding="utf-8")
    investigation_path.write_text(investigation_text, encoding="utf-8")
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    run_markdown_path = output_dir / "runs" / f"{run.manifest.run_id}.md"
    run_markdown_path.parent.mkdir(parents=True, exist_ok=True)
    run_markdown_path.write_text(matrix_text, encoding="utf-8")
    return matrix_path, investigation_path, summary_path


def render_matrix(run: BenchmarkRun) -> str:
    """Return the full benchmark matrix as Markdown."""
    results = {result.case_id: result for result in run.results}
    lines = [
        "# AI Pipeline Core LLM Benchmark Matrix",
        "",
        f"Run ID: `{run.manifest.run_id}`",
        f"Started: `{run.manifest.started_at.isoformat()}`",
        f"Finished: `{run.manifest.finished_at.isoformat() if run.manifest.finished_at else 'incomplete'}`",
        f"Proxy config SHA: `{run.manifest.proxy_config_sha256[:12]}`",
        f"Framework: `{run.manifest.framework_version}`",
        f"Total cost: `${run.manifest.total_cost_usd:.4f}`",
        f"Total calls: `{run.manifest.total_calls}`",
        "",
        "Legend: PASS, PARTIAL, FAIL, SKIP.",
        "",
        "## Logical Model Matrix",
        "",
        _table_header(("model_name", *CANONICAL_PROBES)),
    ]
    for model_name in run.inventory.model_names:
        row = [model_name]
        for probe_key in CANONICAL_PROBES:
            case = next(
                (
                    item
                    for item in run.cases
                    if item.model_name == model_name and item.probe_key == probe_key and item.scope != "deployment"
                ),
                None,
            )
            row.append(_cell(results.get(case.case_id) if case is not None else None))
        lines.append(_table_row(row))

    lines.extend([
        "",
        "## Per-Deployment Cheap Probes",
        "",
        _table_header((
            "deployment_id",
            "model_name",
            "basic_generation",
            "aipl_metadata",
            "cost_sync",
            "routing_telemetry",
            "structured_basemodel",
        )),
    ])
    for deployment in run.inventory.concrete_deployments:
        row = [deployment.deployment_id, deployment.model_name]
        for probe_key in (
            "basic_generation",
            "aipl_metadata",
            "cost_sync",
            "routing_telemetry",
            "structured_basemodel",
        ):
            case = next(
                (
                    item
                    for item in run.cases
                    if item.deployment_id == deployment.deployment_id and item.probe_key == probe_key
                ),
                None,
            )
            row.append(_cell(results.get(case.case_id) if case is not None else None))
        lines.append(_table_row(row))

    lines.extend(["", "## Notes", ""])
    noted = [result for result in run.results if result.notes or result.error_message]
    if not noted:
        lines.append("No notes.")
    for result in noted:
        detail = "; ".join((*result.notes, result.error_message or ""))
        lines.append(f"- `{result.model_name}` `{result.probe_key}` `{result.deployment_id or 'model'}`: {detail}")
    return "\n".join(lines) + "\n"


def render_needs_investigation(run: BenchmarkRun) -> str:
    """Return Markdown listing cells where actual status diverges from expected status."""
    mismatches = [result for result in run.results if result.actual != result.expected]
    lines = ["# LLM Benchmark Needs Investigation", ""]
    if not mismatches:
        lines.append("No unexpected cells.")
        return "\n".join(lines) + "\n"
    for result in mismatches:
        lines.extend([
            f"## {result.model_name} / {result.probe_key}",
            "",
            f"- Deployment: `{result.deployment_id or 'model-level'}`",
            f"- Expected: `{result.expected}`",
            f"- Actual: `{result.actual}`",
            f"- Error category: `{result.error_category or ''}`",
            f"- Error: {result.error_message or ''}",
            f"- AIPL call id: `{result.aipl_call_id or ''}`",
            f"- AIPL deployment id: `{result.aipl_deployment_id or ''}`",
            f"- Provider: `{result.provider or ''}`",
            f"- Suggested next step: {_suggest_next_step(result)}",
            "",
        ])
    return "\n".join(lines)


def render_summary(run: BenchmarkRun) -> dict[str, object]:
    """Return a compact JSON-serializable benchmark summary."""
    counts = Counter(result.actual for result in run.results)
    mismatches = [result.case_id for result in run.results if result.actual != result.expected]
    return {
        "run_id": run.manifest.run_id,
        "total_cost_usd": run.manifest.total_cost_usd,
        "total_calls": run.manifest.total_calls,
        "counts": dict(sorted(counts.items())),
        "needs_investigation": mismatches,
    }


def main() -> None:
    """Run the renderer CLI."""
    parser = argparse.ArgumentParser(description="Render LLM benchmark matrix artifacts.")
    parser.add_argument("input", nargs="?", default=".tmp/benchmark/latest.json", help="BenchmarkRun JSON path")
    args = parser.parse_args()
    paths = render_files(Path(args.input))
    sys.stdout.write("\n".join(str(path) for path in paths) + "\n")


def _table_header(columns: tuple[str, ...]) -> str:
    return _table_row(columns) + "\n" + _table_row(tuple("---" for _ in columns))


def _table_row(values: tuple[object, ...] | list[object]) -> str:
    return "| " + " | ".join(str(value) for value in values) + " |"


def _cell(result: ProbeResult | None) -> str:
    if result is None:
        return ""
    return _SYMBOLS[result.actual]


def _suggest_next_step(result: ProbeResult) -> str:
    if result.probe_key in {"aipl_metadata", "routing_telemetry"}:
        return "Verify AIPL proxy metadata and prompt cache key routing for this deployment."
    if result.probe_key == "search_citations":
        return "Verify provider search grounding and citation extraction."
    if result.actual == "fail":
        return "Check provider health, request shape, and recent proxy logs."
    return "Compare provider capability flags with the latest observed response."


if __name__ == "__main__":
    main()
