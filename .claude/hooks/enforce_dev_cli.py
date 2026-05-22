"""Enforce dev CLI usage and block raw tool invocation.

PreToolUse hook for Bash commands. Exit 2 + stderr blocks. Exit 0 allows.

Blocked:
- pytest, ruff check/format, basedpyright/pyright/mypy → use dev CLI
- removed dev test flags and commands → use lanes/rerun/dev info
- live examples, benchmark, verify, and removed dev commands in agent context
- RUN_BENCHMARK=1 without dev benchmark → use dev benchmark
- uv sync, uv run, uv venv, python -m venv, virtualenv → no venvs
- pytest | grep/head/tail → buffering hangs
"""

import json
import re
import sys

# Matches command position: start of string, after shell operators, after (, or inside shell wrapper quotes.
_CMD = r"(?:^|[;&|('\"]\s*)"
_LIVE_DEV_COMMAND = r"dev\s+(?:verify|benchmark)\b"
_LIVE_DEV_EXAMPLES = r"dev\s+examples\b[^\n;|&]*?--live\b"
_PYTHON_DEV = r"python3?\s+-m\s+dev_cli(?:\.main)?"
_LIVE_PYTHON_DEV_COMMAND = _PYTHON_DEV + r"\s+(?:verify|benchmark)\b"
_LIVE_PYTHON_DEV_EXAMPLES = _PYTHON_DEV + r"\s+examples\b[^\n;|&]*?--live\b"
_LIVE_DIRECT_COMMAND = r"(?:" + "|".join((_LIVE_DEV_COMMAND, _LIVE_DEV_EXAMPLES, _LIVE_PYTHON_DEV_COMMAND, _LIVE_PYTHON_DEV_EXAMPLES)) + r")"
_ENV_OR_COMMAND = r"(?:(?:\w+=\S+\s+)+|env(?:\s+(?:-\S+|\w+=\S+))*\s+|command\s+)"

_PYTEST_BLOCKED = (
    "BLOCKED: Raw pytest bypasses dev's lane, cache, timeout, and log controls.\n"
    "  Do not bypass this hook. Use 'dev test', 'dev test --lane=unit', or 'dev test --rerun-failed'."
)

RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(_CMD + r"dev\s+test\b[^\n;|&]*?--(?:force|no-testmon)\b"),
        "BLOCKED: --force/--no-testmon bypass dev's testmon and cache-safety model.\n  Do not bypass this hook. Use 'dev test' or 'dev test --rerun-failed'.",
    ),
    (
        re.compile(_CMD + r"dev\s+test\b[^\n;|&]*?--(?:full|available|integration|all)\b"),
        "BLOCKED: Removed lane flags bypass the current lane model and can trigger live tests.\n"
        "  Do not bypass this hook. Use 'dev test --lane=unit' or 'dev check'.",
    ),
    # Integration and qualification tests are allowed so agents can validate changes
    # through the dev CLI while still preserving cache, timeout, and runner defaults.
    (
        re.compile(_CMD + r"dev\s+(?:verify|benchmark)\b"),
        "BLOCKED: dev verify/benchmark run broad or live validation outside the agent budget.\n  Do not bypass this hook. Use 'dev check'.",
    ),
    (
        re.compile(_CMD + r"dev\s+examples\b[^\n;|&]*?--live\b"),
        "BLOCKED: Live examples make external LLM calls.\n  Do not bypass this hook. Use 'dev examples --static' or 'dev examples --smoke'.",
    ),
    (
        re.compile(_CMD + r"(?:" + _LIVE_PYTHON_DEV_COMMAND + r"|" + _LIVE_PYTHON_DEV_EXAMPLES + r")"),
        "BLOCKED: Python-module dev entrypoints can bypass live/broad-validation guardrails.\n  Do not bypass this hook. Use 'dev check'.",
    ),
    (
        re.compile(_CMD + _ENV_OR_COMMAND + _LIVE_DIRECT_COMMAND),
        "BLOCKED: Wrapped dev commands can hide live or broad validation from the agent policy.\n  Do not bypass this hook. Use 'dev check'.",
    ),
    (
        re.compile(_CMD + r"dev\s+test\b[^\n;|&]*?--lf\b"),
        "BLOCKED: --lf is removed and does not express the scoped rerun contract.\n  Do not bypass this hook. Use 'dev test --rerun-failed'.",
    ),
    (
        re.compile(_CMD + r"dev\s+(?:ci|list-tests)\b"),
        "BLOCKED: This removed dev command bypasses the current workflow surface.\n"
        "  Do not bypass this hook. Use 'dev check' for validation or 'dev info' for guidance.",
    ),
    (
        re.compile(_CMD + r"dev\s+test\b[^\n;|&]*?(?:--coverage\b|(?:^|\s)(?:-n|--num-workers|--timeout)(?:[=\s]|$))"),
        "BLOCKED: Execution-control flags bypass dev's lane-owned coverage, worker, and timeout settings.\n"
        "  Do not bypass this hook. Use 'dev test --lane=unit' or 'dev check'.",
    ),
    (
        re.compile(_CMD + r"RUN_BENCHMARK=1\s+(?!dev\s+benchmark\b)\S"),
        "BLOCKED: RUN_BENCHMARK=1 can force live benchmark collection outside dev benchmark.\n  Do not bypass this hook. Use 'dev check' in this session.",
    ),
    # dev CLI piping — output is already captured to .tmp/dev-runs/
    # Matches both `dev test | grep` and `dev test 2>&1 | grep`
    (
        re.compile(r"\bdev\s+\S+.*\|\s*(?:grep|head|tail|less|more)\b"),
        "BLOCKED: Piping dev output can hide failures and the full log is already captured.\n"
        "  Do not bypass this hook. Run the dev command directly and read the .tmp/dev-runs log.",
    ),
    # pytest piping — must be checked before the general pytest rule
    (
        re.compile(r"(?:python3?\s+-m\s+)?pytest\s[^|]*\|\s*(?:grep|head|tail|less|more)\b"),
        "BLOCKED: Piped pytest both bypasses dev and can hang through output buffering.\n  Do not bypass this hook. Use 'dev test' and read the captured log.",
    ),
    # raw pytest (but allow --version/--help)
    (
        re.compile(_CMD + r"(?:python3?\s+-m\s+)?pytest\s+(?!--version\b|--help\b|-h\b)"),
        _PYTEST_BLOCKED,
    ),
    # pytest with no args (bare "pytest" at end of string)
    (
        re.compile(_CMD + r"(?:python3?\s+-m\s+)?pytest\s*$", re.MULTILINE),
        _PYTEST_BLOCKED,
    ),
    (
        re.compile(_CMD + r"(?:python3?\s+-m\s+)?ruff\s+(?!--version\b|--help\b|-h\b)(?:check|format)(?:\s|$)"),
        "BLOCKED: Raw ruff bypasses dev's ordered lint/format workflow and logging.\n  Do not bypass this hook. Use 'dev lint', 'dev format', or 'dev check'.",
    ),
    (
        re.compile(_CMD + r"(?:python3?\s+-m\s+)?(?:basedpyright|pyright|mypy)(?:\s+(?!--version\b|--help\b|-h\b)|$)"),
        "BLOCKED: Raw typecheckers bypass dev's configured project/test typecheck flow.\n  Do not bypass this hook. Use 'dev typecheck' or 'dev check'.",
    ),
    (
        re.compile(_CMD + r"uv\s+sync\b"),
        "BLOCKED: uv sync creates a local .venv, but this devcontainer uses system-wide tools.\n"
        "  Do not bypass this hook. Use the installed 'dev' commands already on PATH.",
    ),
    (
        re.compile(_CMD + r"uv\s+run\s"),
        "BLOCKED: uv run is an unnecessary wrapper around tools already on PATH.\n"
        "  Do not bypass this hook. Run the underlying command directly, for example 'dev test'.",
    ),
    (
        re.compile(_CMD + r"(?:uv\s+venv|python3?\s+-m\s+venv|virtualenv)\b"),
        "BLOCKED: Creating a local virtualenv conflicts with the system-wide devcontainer install.\n"
        "  Do not bypass this hook. Use the installed tools already on PATH.",
    ),
)


def check(command: str) -> str | None:
    """Return block message if command is disallowed, None if allowed."""
    for pattern, message in RULES:
        if pattern.search(command):
            return message
    return None


def main() -> None:
    data = json.load(sys.stdin)
    command = data.get("tool_input", {}).get("command", "")
    block_message = check(command)
    if block_message:
        print(block_message, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
