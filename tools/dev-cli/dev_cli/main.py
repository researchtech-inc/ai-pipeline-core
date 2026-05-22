"""Dev CLI entrypoint for test, lint, check, and verification workflows."""

import argparse
import sys
import time

from dev_cli._lane import EXAMPLE_PATHS, LANE_PATHS, LANE_XDIST_ARGS, LANES, TESTMON_LANES, lane_from_path
from dev_cli._project import load_config
from dev_cli._rerun import compute_rerun_set
from dev_cli._runner import run_check_step, run_command
from dev_cli._scope import detect_test_scope, get_source_dirs_for_test_dirs, resolve_scope
from dev_cli._state import check_idempotency, hash_tracked_files, save_run_state


def _relative_runs_dir() -> str:
    cfg = load_config()
    return f"{cfg.runs_dir.relative_to(cfg.repo_root).as_posix()}/"


def _lane_for_scope(scope: str, scope_dirs: list[str]) -> str:
    lanes = {lane_from_path(path) for path in scope_dirs}
    lanes.discard(None)
    if lanes == {"benchmark"}:
        print("ERROR: benchmark scope is run with 'dev benchmark', not 'dev test'.", file=sys.stderr)
        sys.exit(2)
    if not lanes:
        print(f"ERROR: scope '{scope}' does not map to a known test lane.", file=sys.stderr)
        sys.exit(2)
    if len(lanes) > 1:
        print(f"ERROR: scope '{scope}' spans multiple lanes ({', '.join(sorted(lanes))}). Use an explicit lane command.", file=sys.stderr)
        sys.exit(2)
    return next(iter(lanes))


def _lane_for_auto_scope(scope_dirs: list[str]) -> str:
    lanes = {lane_from_path(path) for path in scope_dirs}
    lanes.discard(None)
    if "qualification" in lanes:
        return "qualification"
    if "integration" in lanes:
        return "integration"
    return "unit"


def _build_test_selection(args: argparse.Namespace) -> tuple[str, str, list[str]]:
    if args.scope:
        scope_dirs = resolve_scope(args.scope)
        lane = _lane_for_scope(args.scope, scope_dirs)
        return lane, args.scope, scope_dirs

    if args.lane:
        return args.lane, f"lane-{args.lane}", list(LANE_PATHS[args.lane])

    scope_dirs = resolve_scope(None)
    if not scope_dirs:
        return "unit", "lane-unit", list(LANE_PATHS["unit"])
    lane = _lane_for_auto_scope(scope_dirs)
    return lane, "auto", scope_dirs


def _is_collection_failure_summary(summary: str) -> bool:
    return "collection failed" in summary


def cmd_test(args: argparse.Namespace) -> int:
    """Run tests by scope or by full lane."""
    cfg = load_config()
    lane, scope_label, scope_dirs = _build_test_selection(args)

    cmd = list(cfg.command("pytest"))
    pytest_targets: list[str]
    if args.rerun_failed:
        rerun = compute_rerun_set(cfg.repo_root, tuple(scope_dirs))
        if not rerun.should_run:
            return 0
        cmd.extend(["-p", "no:testmon"])
        pytest_targets = list(rerun.nodeids)
        scope_label = f"{scope_label}-rerun-failed"
    else:
        if lane in TESTMON_LANES:
            cmd.append("--testmon")
        pytest_targets = scope_dirs

    cmd.extend(LANE_XDIST_ARGS[lane])
    cmd.extend(["--tb=short", "--no-header", "-q"])
    cmd.extend(pytest_targets)

    worker_label = " ".join(LANE_XDIST_ARGS[lane])
    command_key = f"test:{lane}:{scope_label}:{worker_label}"
    hash_dirs = [*scope_dirs, *cfg.source_packages]
    source_dirs = get_source_dirs_for_test_dirs(scope_dirs)
    hash_dirs.extend(source_dirs)
    file_hash = hash_tracked_files(*hash_dirs)

    if not args.rerun_failed:
        prev = check_idempotency(command_key, file_hash)
        if prev and not _is_collection_failure_summary(prev.summary):
            from dev_cli._runner import print_skip

            return print_skip(f"test {scope_label}", prev.timestamp, prev.exit_code, prev.log_file)

    exit_code, summary, log_file, is_collection_failure = run_command(
        cmd,
        f"test {scope_label}",
        log_suffix=f"test-{scope_label}",
        use_reportlog=True,
    )
    if not is_collection_failure:
        save_run_state(command_key, " ".join(cmd), exit_code, summary, log_file, file_hash)
    print(summary)
    return exit_code


def cmd_lint(args: argparse.Namespace) -> int:
    """Run ruff check and format check."""
    cfg = load_config()

    if not cfg.has_ruff:
        print("SKIP  lint - ruff not found")
        return 0

    if args.fix:
        return cmd_format(args)

    exit_code, summary, log_file, _ = run_command(cfg.command("ruff", "check", "."), "lint:check", log_suffix="lint-check")
    if exit_code != 0:
        print(summary)
        print("\n  Auto-fix: dev format")
        return exit_code

    exit_code2, summary2, log_file2, _ = run_command(cfg.command("ruff", "format", "--check", "."), "lint:format", log_suffix="lint-format")
    if exit_code2 != 0:
        print(summary2)
        print("\n  Auto-fix: dev format")
        return exit_code2

    print("PASS  lint")
    print(f"  Details: {log_file}, {log_file2}")
    return 0


def cmd_format(args: argparse.Namespace) -> int:
    """Auto-fix ruff formatting and lint issues."""
    cfg = load_config()

    if not cfg.has_ruff:
        print("SKIP  format - ruff not found")
        return 0

    exit_code1, summary1, log_file1, _ = run_command(cfg.command("ruff", "format", "."), "format:ruff-format", log_suffix="format-fmt")
    exit_code2, summary2, log_file2, _ = run_command(cfg.command("ruff", "check", "--fix", "."), "format:ruff-fix", log_suffix="format-fix")

    if exit_code1 != 0 or exit_code2 != 0:
        print(summary1 if exit_code1 != 0 else summary2)
        return exit_code1 or exit_code2

    print("PASS  format - all files formatted and auto-fixed")
    print(f"  Details: {log_file1}, {log_file2}")
    return 0


def cmd_typecheck(args: argparse.Namespace) -> int:
    """Run the configured type checker."""
    cfg = load_config()
    log_files: list[str] = []

    if cfg.has_basedpyright:
        exit_code, summary, log_file, _ = run_command(cfg.command("basedpyright", "--level", "warning"), "typecheck:source", log_suffix="typecheck-src")
        log_files.append(log_file)
        if exit_code != 0:
            print(summary)
            return exit_code

        if (cfg.repo_root / "pyrightconfig.tests.json").exists():
            exit_code2, summary2, log_file2, _ = run_command(
                cfg.command("basedpyright", "--level", "error", "-p", "pyrightconfig.tests.json"),
                "typecheck:tests",
                log_suffix="typecheck-tests",
            )
            log_files.append(log_file2)
            if exit_code2 != 0:
                print(summary2)
                return exit_code2
    elif cfg.has_pyright:
        exit_code, summary, log_file, _ = run_command(cfg.command("pyright"), "typecheck", log_suffix="typecheck")
        log_files.append(log_file)
        if exit_code != 0:
            print(summary)
            return exit_code
    elif cfg.has_mypy:
        exit_code, summary, log_file, _ = run_command(cfg.command("mypy", "."), "typecheck", log_suffix="typecheck")
        log_files.append(log_file)
        if exit_code != 0:
            print(summary)
            return exit_code
    else:
        print("SKIP  typecheck - no type checker found (basedpyright, pyright, mypy)")
        return 0

    print("PASS  typecheck")
    print(f"  Details: {', '.join(log_files)}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Run checks in order."""
    cfg = load_config()

    check_start = time.monotonic()

    for step in cfg.checks:
        exit_code = run_check_step(step.name, step.commands)
        if exit_code != 0:
            return exit_code

    total = time.monotonic() - check_start
    print(f"\nPASS  check - all checks passed ({total:.1f}s)")
    print(f"  Details: {_relative_runs_dir()}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Run the validation sequence."""
    cfg = load_config()
    steps = (
        ("verify-check", cfg.command("dev", "check")),
        ("verify-integration", cfg.command("dev", "test", "--lane=integration")),
        ("verify-examples-smoke", cfg.command("dev", "examples", "--smoke")),
    )
    for label, command in steps:
        exit_code, summary, _, _ = run_command(command, label, log_suffix=label)
        print(summary)
        if exit_code != 0:
            return exit_code
    print("PASS  verify")
    print(f"  Details: {_relative_runs_dir()}")
    return 0


def cmd_benchmark(args: argparse.Namespace) -> int:
    """Run benchmark matrix tiers through the dev wrapper."""
    cfg = load_config()
    env = {"RUN_BENCHMARK": "1"}
    action = "benchmark"
    if args.smoke:
        env["BENCHMARK_TIER"] = "smoke"
        action = "benchmark-smoke"
    elif args.cases:
        env["BENCHMARK_CASE_GLOB"] = args.cases
        action = "benchmark-cases"
    else:
        env["BENCHMARK_TIER"] = args.tier
        action = f"benchmark-{args.tier}"

    cmd = list(cfg.command("pytest", "-q", "--tb=short", "--no-header", "benchmarks/llm_matrix/"))
    exit_code, summary, _, _ = run_command(cmd, action, log_suffix=action, use_reportlog=True, env=env)
    print(summary)
    return exit_code


def cmd_capabilities(args: argparse.Namespace) -> int:
    """Run model capability probes against a runtime model list."""
    cfg = load_config()
    models = (args.models or "").strip()
    if not models:
        print(
            "ERROR: dev capabilities requires --models <csv> (e.g. 'gpt-5.4-mini,gemini-3-flash').",
            file=sys.stderr,
        )
        return 2
    env = {"CAPABILITIES_MODELS": models}
    cmd = list(cfg.command("pytest", "-q", "--tb=short", "--no-header", "-s", "tests/capabilities/"))
    exit_code, summary, _, _ = run_command(
        cmd,
        "capabilities",
        log_suffix="capabilities",
        use_reportlog=True,
        env=env,
    )
    print(summary)
    return exit_code


def cmd_examples(args: argparse.Namespace) -> int:
    """Run examples validation tiers."""
    cfg = load_config()
    if args.live:
        tier = "live"
        xdist = ("-n", "2")
    elif args.smoke:
        tier = "smoke"
        xdist = LANE_XDIST_ARGS["unit"]
    else:
        tier = "static"
        xdist = LANE_XDIST_ARGS["unit"]

    target = EXAMPLE_PATHS[tier]
    if tier == "live" and not (cfg.repo_root / target).exists():
        print("SKIP examples-live: test_live.py not present (Phase 7)")
        return 0
    cmd = list(cfg.command("pytest", *xdist, "--tb=short", "--no-header", "-q", target))
    exit_code, summary, _, _ = run_command(cmd, f"examples-{tier}", log_suffix=f"examples-{tier}", use_reportlog=True)
    print(summary)
    return exit_code


def cmd_status(args: argparse.Namespace) -> int:
    """Show changed files, affected scopes, and recent run state."""
    cfg = load_config()

    test_dirs, source_dirs = detect_test_scope()

    print("Changed source modules:")
    if source_dirs:
        for directory in source_dirs:
            print(f"  {directory}")
    else:
        print("  (none)")

    print("\nAffected test directories:")
    if test_dirs:
        for directory in test_dirs:
            print(f"  {directory}")
    else:
        print("  (none detected)")

    if cfg.state_file.exists():
        import json

        try:
            data = json.loads(cfg.state_file.read_text())
            runs = data.get("runs", {})
            if runs:
                print("\nLast runs:")
                for key, run in runs.items():
                    status = "PASS" if run["exit_code"] == 0 else "FAIL"
                    print(f"  {key}: {status} at {run['timestamp'][:19]}")
        except json.JSONDecodeError:
            pass

    print("\nSuggested next command:")
    if test_dirs:
        if len(test_dirs) == 1:
            alias = test_dirs[0].split("/")[-1]
            print(f"  dev test {alias}")
        else:
            print("  dev test --lane=unit")
    else:
        print("  dev test --lane=unit")

    return 0


INFO_TEXT = """\
dev - Development CLI for test/lint/check workflows.

Commands:
  dev test [SCOPE] [--lane {unit,integration,qualification}] [--rerun-failed]
  dev format
  dev lint
  dev typecheck
  dev check
  dev verify
  dev examples (--static | --smoke | --live)
  dev benchmark (--smoke | --cases GLOB | --tier {full,nightly})
  dev capabilities --models <csv>
  dev status
  dev info
"""


def cmd_info(args: argparse.Namespace) -> int:
    """Print usage, lanes, checks, scopes, and infrastructure status."""
    cfg = load_config()

    print(INFO_TEXT)
    print("Logs:")
    print("  Details: .tmp/dev-runs/<YYYYMMDD-HHMMSS>-<label>.log")
    print("  Pytest runs also write matching .tmp/dev-runs/<YYYYMMDD-HHMMSS>-<label>.jsonl report logs.")
    print("  .tmp/dev-runs/.state.json stores idempotency and step-cache metadata.")
    print()
    print("Test lanes:")
    print("  unit")
    print("  integration")
    print("  qualification")
    print("Examples:")
    print("  static / smoke / live")
    print("Benchmark:")
    print("  smoke / --cases / --tier full / --tier nightly")
    print()

    print("Auto-detected check pipeline (dev check):")
    for i, step in enumerate(cfg.checks, 1):
        cmds_str = " && ".join(" ".join(c) for c in step.commands)
        print(f"  {i}. {step.name}: {step.description}")
        print(f"     {cmds_str}")
    print()

    print(f"Available test scopes: {', '.join(sorted(cfg.scope_aliases))}")
    print(f"Runner: {' '.join(cfg.runner_prefix) or '(bare)'}")

    if cfg.infrastructure:
        from dev_cli._infra import detect, format_status

        print()
        statuses = detect(cfg.infrastructure, cfg.repo_root)
        print(format_status(statuses))

    return 0


def build_parser() -> argparse.ArgumentParser:
    cfg = load_config()
    scope_names = ", ".join(sorted(cfg.scope_aliases))

    parser = argparse.ArgumentParser(
        prog="dev",
        description="Development CLI - enforces correct test/lint/check workflows.",
    )
    sub = parser.add_subparsers(dest="command")

    p_test = sub.add_parser("test", help="Run tests by scope or lane")
    test_target = p_test.add_mutually_exclusive_group()
    test_target.add_argument("scope", nargs="?", help=f"Test scope: {scope_names}")
    test_target.add_argument("--lane", choices=LANES, default=None, help="Run a full lane")
    p_test.add_argument("--rerun-failed", action="store_true", help="Rerun filtered last-failed tests")
    p_test.set_defaults(func=cmd_test)

    p_lint = sub.add_parser("lint", help="Check lint")
    p_lint.add_argument("--fix", action="store_true", help="Auto-fix (same as 'dev format')")
    p_lint.set_defaults(func=cmd_lint)

    p_format = sub.add_parser("format", help="Auto-fix formatting and lint")
    p_format.set_defaults(func=cmd_format)

    p_tc = sub.add_parser("typecheck", help="Run type checker")
    p_tc.set_defaults(func=cmd_typecheck)

    p_check = sub.add_parser("check", help="Run checks")
    p_check.set_defaults(func=cmd_check)

    p_verify = sub.add_parser("verify", help="Run validation")
    p_verify.set_defaults(func=cmd_verify)

    p_capabilities = sub.add_parser("capabilities", help="Probe AIModel capabilities against a model list")
    p_capabilities.add_argument(
        "--models",
        required=True,
        metavar="CSV",
        help="Comma-separated model names to probe (e.g. 'gpt-5.4-mini,gemini-3-flash')",
    )
    p_capabilities.set_defaults(func=cmd_capabilities)

    p_benchmark = sub.add_parser("benchmark", help="Run benchmark matrix tiers")
    benchmark_group = p_benchmark.add_mutually_exclusive_group(required=True)
    benchmark_group.add_argument("--smoke", action="store_true", help="Run representative benchmark cells")
    benchmark_group.add_argument("--cases", metavar="GLOB", help="Run benchmark cases matching a glob")
    benchmark_group.add_argument("--tier", choices=("full", "nightly"), help="Run a release benchmark tier")
    p_benchmark.set_defaults(func=cmd_benchmark)

    p_examples = sub.add_parser("examples", help="Run examples validation tiers")
    examples_group = p_examples.add_mutually_exclusive_group(required=True)
    examples_group.add_argument("--static", action="store_true", help="Run static examples checks")
    examples_group.add_argument("--smoke", action="store_true", help="Run smoke examples checks")
    examples_group.add_argument("--live", action="store_true", help="Run live examples checks")
    p_examples.set_defaults(func=cmd_examples)

    p_status = sub.add_parser("status", help="Show changed files, last runs, suggestions")
    p_status.set_defaults(func=cmd_status)

    p_info = sub.add_parser("info", help="Show detailed usage and project state")
    p_info.set_defaults(func=cmd_info)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command is None:
        args = argparse.Namespace()
        exit_code = cmd_info(args)
    else:
        exit_code = args.func(args)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
