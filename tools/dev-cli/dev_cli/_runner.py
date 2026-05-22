"""Subprocess execution with output capture and summary formatting."""

import json
import os
import re
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

MAX_INLINE_FAILURES = 5
SLOW_TEST_THRESHOLD_SECONDS = 30
MAX_RUN_FILE_AGE_SECONDS = 86400  # 24 hours
CACHEABLE_CHECK_STEPS = frozenset({"lint", "typecheck", "deadcode"})


_cleanup_done = False


def _cleanup_old_runs() -> None:
    """Remove run artifacts older than 24 hours. Best-effort, silent."""
    global _cleanup_done
    if _cleanup_done:
        return
    _cleanup_done = True

    from dev_cli._project import load_config

    cfg = load_config()
    if not cfg.runs_dir.is_dir():
        return

    cutoff = time.time() - MAX_RUN_FILE_AGE_SECONDS
    deleted_any = False

    for entry in cfg.runs_dir.iterdir():
        if entry.name == ".state.json":
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
                deleted_any = True
        except OSError:
            continue

    if deleted_any:
        _prune_stale_state_entries(cfg)


def _prune_stale_state_entries(cfg) -> None:
    """Remove state entries whose log files no longer exist on disk."""
    if not cfg.state_file.exists():
        return
    try:
        data = json.loads(cfg.state_file.read_text())
        runs = data.get("runs", {})
        pruned = {k: v for k, v in runs.items() if (cfg.repo_root / v.get("log_file", "")).exists()}
        if len(pruned) < len(runs):
            data["runs"] = pruned
            cfg.state_file.write_text(json.dumps(data, indent=2))
    except (json.JSONDecodeError, OSError):  # fmt: skip
        pass


TEST_TIMEOUT_SECONDS = 300  # 5 minutes


def run_command(
    cmd: list[str] | tuple[str, ...],
    label: str,
    *,
    log_suffix: str | None = None,
    use_reportlog: bool = False,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str, bool]:
    """Run a command, capture output to file, print summary.

    Returns (exit_code, summary_line, log_file_relative_path, is_collection_failure).
    """
    _cleanup_old_runs()

    from dev_cli._project import load_config

    cfg = load_config()
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(UTC)
    suffix = log_suffix or label.replace(" ", "-")
    log_filename = f"{now:%Y%m%d-%H%M%S}-{suffix}.log"
    log_path = cfg.runs_dir / log_filename
    rel_log = str(log_path.relative_to(cfg.repo_root))

    report_path = cfg.runs_dir / f"{now:%Y%m%d-%H%M%S}-{suffix}.jsonl" if use_reportlog else None

    full_cmd = list(cmd)
    if use_reportlog and report_path:
        full_cmd.extend(["--report-log", str(report_path)])

    try:
        run_env = os.environ.copy()
        if env:
            run_env.update(env)
        with open(log_path, "w") as f:
            f.write(f"$ {' '.join(full_cmd)}\n")
            f.write(f"# timestamp: {now.isoformat()}\n\n")
            f.flush()
            result = subprocess.run(
                full_cmd,
                stdout=f,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=cfg.repo_root,
                env=run_env,
                timeout=TEST_TIMEOUT_SECONDS,
            )
            f.write(f"\n# exit code: {result.returncode}\n")
    except subprocess.TimeoutExpired:
        with open(log_path, "a") as f:
            f.write(f"\n# timeout after {TEST_TIMEOUT_SECONDS}s\n")
        summary = f"FAIL  {label} — timeout after {TEST_TIMEOUT_SECONDS}s\n  Details: {rel_log}"
        return 124, summary, rel_log, False
    except FileNotFoundError:
        tool_name = full_cmd[0]
        with open(log_path, "a") as f:
            f.write(f"\n# file not found: {tool_name}\n")
        summary = f"FAIL  {label} — '{tool_name}' not found\n  Details: {rel_log}\n  Install it or check your PATH. Command was: {' '.join(full_cmd)}"
        return 127, summary, rel_log, False

    log_text = log_path.read_text(encoding="utf-8", errors="replace")

    if use_reportlog and report_path and report_path.exists():
        summary, is_collection_failure = _summarize_pytest_report(report_path, result.returncode, rel_log, log_text)
    else:
        summary = _summarize_generic(result, label, rel_log, log_text)
        is_collection_failure = False

    return result.returncode, summary, rel_log, is_collection_failure


def run_check_step(step_name: str, commands: tuple[tuple[str, ...], ...]) -> int:
    """Run a dev check step, using the per-step cache for safe steps."""
    from dev_cli._project import load_config
    from dev_cli._state import hash_tracked_files, load_step_cache, save_step_cache

    cfg = load_config()
    started = time.monotonic()
    cacheable = step_name in CACHEABLE_CHECK_STEPS
    input_hash = hash_tracked_files(*cfg.source_packages, "tests", "examples", "benchmarks", "tools/dev-cli", "tools/trace-inspector")
    tool_version = _tool_version_for_commands(commands)
    if cacheable:
        cached = load_step_cache(step_name)
        if (
            cached is not None
            and cached.input_hash == input_hash
            and cached.tool_version == tool_version
            and cached.result == "PASS"
            and (cfg.repo_root / cached.log_file).exists()
        ):
            print(f"SKIP  {step_name} (cached)")
            print(f"  Details: {cached.log_file}")
            return 0

    last_log_file = ""
    for cmd_tuple in commands:
        exit_code, summary, log_file, _ = run_command(list(cmd_tuple), step_name, log_suffix=f"check-{step_name}")
        last_log_file = log_file
        if exit_code != 0:
            print(summary)
            return exit_code

    if cacheable:
        save_step_cache(step_name, input_hash, tool_version, "PASS", last_log_file)
    elapsed = time.monotonic() - started
    print(f"PASS  {step_name} ({elapsed:.1f}s)")
    print(f"  Details: {last_log_file}")
    return 0


def _tool_version_for_commands(commands: tuple[tuple[str, ...], ...]) -> str:
    parts: list[str] = []
    for command in commands:
        version_cmd = _version_command(command)
        try:
            result = subprocess.run(version_cmd, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:  # fmt: skip
            parts.append(f"{' '.join(version_cmd)}: {exc}")
            continue
        parts.append(f"{' '.join(version_cmd)}: {result.stdout.strip() or result.stderr.strip()}")
    return "\n".join(parts)


def _version_command(command: tuple[str, ...]) -> list[str]:
    if len(command) >= 3 and command[0] in {"uv", "poetry"} and command[1] == "run":
        return [command[0], command[1], command[2], "--version"]
    return [command[0], "--version"]


_DESELECTED_RE = re.compile(r"(\d+) deselected")


def _parse_deselected_count(stdout: str) -> int:
    """Extract deselected count from pytest stdout (e.g., '54 deselected')."""
    match = _DESELECTED_RE.search(stdout)
    return int(match.group(1)) if match else 0


def _summarize_pytest_report(report_path: Path, exit_code: int, log_file: str, stdout: str = "") -> tuple[str, bool]:
    """Summarize pytest results from reportlog JSONL."""
    passed = 0
    failed = 0
    errors = 0
    skipped = 0
    failures: list[dict] = []
    collect_errors: list[str] = []
    slow_tests: list[tuple[str, float]] = []
    total_duration = 0.0
    first_start: float | None = None
    last_stop: float | None = None

    for line in report_path.read_text().splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        report_type = entry.get("$report_type")

        if report_type == "TestReport" and entry.get("when") == "call":
            outcome = entry.get("outcome", "")
            duration = entry.get("duration", 0.0)
            total_duration += duration

            start = entry.get("start")
            stop = entry.get("stop")
            if start is not None and (first_start is None or start < first_start):
                first_start = start
            if stop is not None and (last_stop is None or stop > last_stop):
                last_stop = stop

            if outcome == "passed":
                passed += 1
            elif outcome == "failed":
                failed += 1
                failures.append({
                    "nodeid": entry.get("nodeid", "?"),
                    "message": _extract_failure_message(entry),
                })
            elif outcome == "skipped":
                skipped += 1

            if duration >= SLOW_TEST_THRESHOLD_SECONDS:
                slow_tests.append((entry.get("nodeid", "?"), duration))

        elif report_type == "TestReport" and entry.get("when") == "setup" and entry.get("outcome") == "failed":
            errors += 1
            failures.append({
                "nodeid": entry.get("nodeid", "?"),
                "message": f"SETUP ERROR: {_extract_failure_message(entry)}",
            })

        elif report_type == "CollectReport" and entry.get("outcome") == "failed":
            nodeid = entry.get("nodeid", "?")
            msg = _extract_failure_message(entry)
            collect_errors.append(f"{nodeid}: {msg}" if msg else nodeid)

    total = passed + failed + errors + skipped
    deselected = _parse_deselected_count(stdout)
    wall_time = (last_stop - first_start) if first_start is not None and last_stop is not None else total_duration
    duration_str = f"{wall_time:.1f}s"

    if failed:
        from dev_cli._project import load_config
        from dev_cli._rerun import record_failure_dependency_hashes

        record_failure_dependency_hashes(load_config().repo_root, tuple(str(f["nodeid"]) for f in failures))

    # Collection failure — no tests ran
    if total == 0 and exit_code != 0:
        lines = [f"FAIL  test — collection failed (exit code {exit_code})"]
        lines.append(f"  Details: {log_file}")
        if collect_errors:
            lines.append("")
            for err in collect_errors[:MAX_INLINE_FAILURES]:
                lines.append(f"  COLLECT ERROR: {err}")
            if len(collect_errors) > MAX_INLINE_FAILURES:
                lines.append(f"  ... and {len(collect_errors) - MAX_INLINE_FAILURES} more")
        return "\n".join(lines), True

    parts = []
    if passed:
        parts.append(f"{passed} passed")
    if failed:
        parts.append(f"{failed} failed")
    if errors:
        parts.append(f"{errors} errors")
    if skipped:
        parts.append(f"{skipped} skipped")
    counts = ", ".join(parts) if parts else f"{total} tests"

    lines: list[str] = []

    if exit_code == 0:
        if deselected:
            lines.append(f"PASS  test — {counts}, {deselected} unchanged (testmon) in {duration_str}")
        else:
            lines.append(f"PASS  test — {counts} in {duration_str}")
        lines.append(f"  Details: {log_file}")
        if slow_tests:
            slow_tests.sort(key=lambda x: -x[1])
            lines.append("")
            lines.append(f"  Slow tests (>{SLOW_TEST_THRESHOLD_SECONDS}s):")
            for nodeid, dur in slow_tests[:MAX_INLINE_FAILURES]:
                lines.append(f"    {nodeid} ({dur:.1f}s)")
            if len(slow_tests) > MAX_INLINE_FAILURES:
                lines.append(f"    ... and {len(slow_tests) - MAX_INLINE_FAILURES} more")
    else:
        lines.append(f"FAIL  test — {counts} in {duration_str}")
        lines.append(f"  Details: {log_file}")
        lines.append("")
        for f in failures[:MAX_INLINE_FAILURES]:
            lines.append(f"  FAIL {f['nodeid']}")
            if f["message"]:
                lines.append(f"       {f['message']}")
        if len(failures) > MAX_INLINE_FAILURES:
            lines.append(f"  ... and {len(failures) - MAX_INLINE_FAILURES} more failures")
        if failed > 0:
            lines.append("")
            lines.append("  Next step:   dev test --rerun-failed")

    return "\n".join(lines), False


def _extract_failure_message(entry: dict) -> str:
    """Extract a short failure message from a TestReport entry."""
    longrepr = entry.get("longrepr", "")
    if isinstance(longrepr, str):
        for line in reversed(longrepr.splitlines()):
            stripped = line.strip()
            if stripped.startswith("E "):
                return stripped[2:].strip()[:120]
        for line in longrepr.splitlines():
            stripped = line.strip()
            if stripped:
                return stripped[:120]
    elif isinstance(longrepr, dict):
        crash = longrepr.get("reprcrash", {})
        return crash.get("message", "")[:120]
    return ""


def _summarize_generic(result: subprocess.CompletedProcess, label: str, log_file: str, output: str = "") -> str:
    """Summarize a non-pytest command result."""
    if result.returncode == 0:
        return f"PASS  {label}\n  Details: {log_file}"

    lines = [f"FAIL  {label} (exit code {result.returncode})"]
    lines.append(f"  Details: {log_file}")

    output_lines = [l for l in output.splitlines() if l.strip() and not l.startswith(("$ ", "# "))]
    if output_lines:
        lines.append("")
        for line in output_lines[-5:]:
            lines.append(f"  {line.rstrip()}")

    return "\n".join(lines)


MAX_WORST_FILES = 5


def format_coverage_summary(runs_dir: Path, scoped: bool) -> str | None:
    """Parse coverage.json and return a concise summary."""
    cov_file = runs_dir / "coverage.json"
    if not cov_file.is_file():
        return None

    data = json.loads(cov_file.read_text())
    total_pct = data["totals"]["percent_covered"]

    files = [(path, info["summary"]["percent_covered"]) for path, info in data["files"].items()]
    files.sort(key=lambda x: x[1])
    worst = files[:MAX_WORST_FILES]

    lines: list[str] = []
    if scoped:
        lines.append(f"  Coverage: {total_pct:.1f}% (scoped run — threshold not enforced)")
    else:
        lines.append(f"  Coverage: {total_pct:.1f}%")

    if worst:
        lines.append("  Lowest:")
        for path, pct in worst:
            lines.append(f"    {path} ({pct:.0f}%)")

    lines.append(f"  HTML: {runs_dir / 'coverage-html' / 'index.html'}")
    return "\n".join(lines)


def print_skip(label: str, previous_timestamp: str, previous_exit_code: int, log_file: str) -> int:
    """Print skip message when no code changed since last run. Returns previous exit code."""
    status = "PASS" if previous_exit_code == 0 else "FAIL"
    print(f"SKIP  {label} — no changes since last run ({status} at {previous_timestamp})")
    print(f"  Details: {log_file}")
    if previous_exit_code != 0:
        print("  Rerun failures: dev test --rerun-failed")
    else:
        print("  All tests already verified. No action needed.")
    return previous_exit_code
