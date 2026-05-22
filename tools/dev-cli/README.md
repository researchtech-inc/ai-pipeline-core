# dev-cli

A development CLI that enforces correct test/lint/check workflows for AI coding agents.

## Problem

AI coding agents (Claude Code, Codex, etc.) consistently misuse developer tools during coding sessions:

- **Pipe pytest output through grep/head/tail** — causes buffering hangs, loses exit codes
- **Run full test suite for single-module changes** — wastes 3-5 minutes per run
- **Rerun failing commands without code changes** — pure waste
- **Use the supported rerun path** — stale last-failed state can cross lanes if reruns are not scoped
- **Run tests before linters** — waits minutes to discover syntax errors that lint catches in seconds
- **Ignore make targets** — manually constructs commands with wrong flags
- **Filter basedpyright through grep** — loses exit codes, hides real errors

Analysis of 8,694 bash commands across 137 Claude Code sessions confirmed these patterns occur hundreds of times despite explicit CLAUDE.md instructions prohibiting them. **AI agents don't follow instructions — they need environmental constraints.**

## Solution

`dev` is a single CLI tool that replaces direct use of `pytest`, `ruff`, `basedpyright`, and `make check`. It:

1. **Captures output to files** — full logs go to `.tmp/dev-runs/`, only a concise summary prints to stdout. Eliminates the need to pipe through grep/head/tail.
2. **Enforces correct flags** — always uses `-x`, `--tb=short`, correct marker expressions, proper basedpyright config.
3. **Detects unchanged code** — hashes source+test files with xxhash, skips reruns when nothing changed.
4. **Auto-scopes tests** — detects which test directories are affected by git changes.
5. **Orders checks correctly** — fast checks (lint, typecheck) run before slow checks (tests).
6. **Suggests next steps** — on failure, prints exactly what command to run next (e.g., `dev test --rerun-failed`).
7. **Reports step durations** — `dev check` shows per-step and total wall-clock time.
8. **Warns about slow tests** — tests exceeding 30s are flagged after the summary (sorted slowest-first, capped at 5).
9. **Auto-cleans old runs** — files in `.tmp/dev-runs/` older than 24h are deleted automatically on the next run. Stale `.state.json` entries are pruned too.

## Usage

```bash
# Show detailed usage guide, auto-detected config, infrastructure status
dev

# Run tests (auto-detect scope from git changes, uses testmon)
dev test

# Run tests for a specific module
dev test pipeline
dev test database
dev test llm

# Rerun last-failed tests within the selected scope
dev test --rerun-failed

# Run a full lane
dev test --lane=unit
dev test --lane=integration
dev test --lane=qualification

# Lint check
dev lint

# Auto-fix lint + formatting
dev format

# Type checking (basedpyright with correct config)
dev typecheck

# All checks in order (lint → typecheck → deadcode → docstrings → test)
dev check

# Show changed files, last run results, suggested next command
dev status

# Detailed usage guide (same as bare 'dev')
dev info
```

## Output Format

Concise summaries designed for AI consumption — no decorative boxes, no wasted tokens:

```
PASS  test — 27 passed, 244 unchanged (testmon) in 1.9s
  Details: .tmp/dev-runs/20260314-130918-test-pipeline.log
```

Testmon tracks dependencies — only tests affected by code changes run. The "unchanged" count shows tests that passed previously and whose dependencies haven't changed. All tests in the scope are verified.

```
PASS  test — 271 passed in 45.3s
  Details: .tmp/dev-runs/20260314-130918-test-pipeline.log

  Slow tests (>30s):
    test_pipeline.py::test_large_batch (42.1s)
    test_database.py::test_bulk_insert (31.7s)
```

```
FAIL  test — 2 failed, 269 passed in 2.1s
  Details: .tmp/dev-runs/20260314-130918-test-pipeline.log

  FAIL test_task.py::test_retry_on_failure
       AssertionError: expected 3 retries, got 1

  FAIL test_task.py::test_timeout_handling
       TimeoutError: task exceeded 30s timeout

  Next step:   dev test --rerun-failed
```

```
SKIP  test pipeline — no changes since last run (PASS at 2026-03-14T13:09:18)
  Details: .tmp/dev-runs/20260314-130918-test-pipeline.log
  All tests already verified. No action needed.
```

`dev check` shows per-step durations and a total:

```
PASS  lint (1.2s)
  Details: .tmp/dev-runs/20260314-130900-check-lint.log
PASS  typecheck (3.4s)
  Details: .tmp/dev-runs/20260314-130902-check-typecheck.log
PASS  deadcode (0.8s)
  Details: .tmp/dev-runs/20260314-130905-check-deadcode.log
PASS  semgrep (2.1s)
  Details: .tmp/dev-runs/20260314-130906-check-semgrep.log
PASS  docstrings (0.5s)
  Details: .tmp/dev-runs/20260314-130908-check-docstrings.log
PASS  test (45.3s)
  Details: .tmp/dev-runs/20260314-130909-check-test.log

PASS  check — all checks passed (53.3s)
  Details: .tmp/dev-runs/
```

Detailed output is always saved to `.tmp/dev-runs/` and surfaced with a `Details:` line when a command prints a result summary. Files older than 24 hours are automatically cleaned up.

## Auto-Detection (Generic Design)

The tool has no hardcoded project configuration. Everything is auto-detected:

| What | How |
|------|-----|
| Repo root | Walk up from cwd looking for `pyproject.toml` / `.git` |
| Source packages | Find dirs with `__init__.py` at root or under `src/` |
| Test scopes | Subdirectories of `tests/` become scope names |
| Source→test mapping | Match source subdir names to test subdir names |
| Markers | Read from `[tool.pytest.ini_options].markers` in pyproject.toml |
| Test groups | Auto-exclude heavy markers (integration, clickhouse, etc.) for unit group |
| Available tools | Check PATH for ruff, basedpyright, pyright, mypy, vulture, etc. |
| Runner prefix | Detect uv.lock → `uv run`, poetry.lock → `poetry run`, else bare |
| CI check steps | Auto-build from detected tools, or read from `[tool.dev-cli.checks]` |

## Optional Configuration

Override auto-detection via `[tool.dev-cli]` in `pyproject.toml`:

```toml
[tool.dev-cli]
cache_dir = ".tmp/dev-runs"
tracked_extensions = [".py", ".toml"]

# Source dirs that map to non-obvious test dirs
[tool.dev-cli.source_to_test]
"ai_pipeline_core/database" = ["tests/database", "tests/deployment", "tests/replay"]
"ai_pipeline_core/_llm_core" = ["tests/llm"]

# Short aliases for 'dev test <alias>'
[tool.dev-cli.scopes]
db = "tests/database"
deploy = "tests/deployment"

# Custom CI check steps (overrides auto-detection)
# [[tool.dev-cli.checks]]
# name = "lint"
# run = [["ruff", "check", "."], ["ruff", "format", "--check", "."]]
```

## Key Design Decision: No pytest addopts

This tool requires that `pyproject.toml` does NOT set `addopts` with behavioral flags (`--quiet`, `--testmon`, `-m '...'`). The `dev` CLI owns all test behavior — having two sources of truth causes conflicts (the CLI has to parse, strip, and override addopts, which is fragile).

Keep in pyproject.toml only engine/metadata config: `asyncio_mode`, `strict`, `testpaths`, `markers`.

## Enforcement

AI agents bypass written instructions — enforcement requires environmental constraints.

A `PreToolUse` hook (`.claude/hooks/enforce_dev_cli.py`) blocks raw tool invocations with actionable error messages pointing to the correct `dev` command. The same script is wired into both agents:

- **Claude Code** loads it via `.claude/settings.json`.
- **Codex CLI** loads it via `.codex/config.toml`. The Codex `PreToolUse` JSON shape (`tool_input.command` for Bash, exit-code-2 + stderr to block) matches Claude Code's, so no per-agent shim is needed.

### Codex CLI hook trust

Codex will not load `.codex/config.toml` (and therefore will not run the hook) until you mark the repo trusted. One-time setup per developer:

1. From the repo root, run `codex`. Accept the trust prompt when Codex shows the project for the first time. This writes `projects.<repo-path>.trust_level = "trusted"` to your `~/.codex/config.toml`.
2. Inside a Codex session, run `/hooks` and confirm a `PreToolUse` matcher `^Bash$` whose command resolves `.claude/hooks/enforce_dev_cli.py` from the git root is listed and trusted.
3. Smoke test: ask the agent to run `pytest tests/unit`. Expected outcome: Codex reports the `BLOCKED: Raw pytest …` message from the hook, no pytest output.

The hook is a guardrail, not a sandbox — it catches the specific raw-tool patterns listed below before they run, but it is not a substitute for Codex's `sandbox_mode` / `approval_policy` or Claude Code's permission system. Codex's `PreToolUse` coverage today is shell-focused; non-shell tool paths (file edits, web search, some MCP tools) are intentionally out of scope here.

### What the hook blocks

Each BLOCKED message names a specific replacement command. Read the message — do not guess a workaround.

**Raw tool invocations** — bypass `dev`'s output capture, lane controls, and configured flags:

- `pytest …` (any args except `--version`, `--help`, `-h`) → `dev test`, `dev test --lane=unit`, or `dev test --rerun-failed`
- `ruff check` / `ruff format` → `dev lint`, `dev format`, or `dev check`
- `basedpyright` / `pyright` / `mypy` → `dev typecheck` or `dev check`
- `python -m dev_cli …` module-mode entrypoints that target live tests, `verify`, `benchmark`, or `--live` examples → `dev test --lane=unit` or `dev check`

**Live, broad, or paid `dev` commands** — must not run from an agent session:

- `dev verify`, `dev benchmark` → `dev check`
- `RUN_BENCHMARK=1 …` (anything other than `dev benchmark`) → `dev check`
- `dev examples --live` → `dev examples --static` or `dev examples --smoke`
- `dev test --lane=integration` / `dev test --lane=qualification` → `dev test --lane=unit` or `dev check`
- `dev test tests/integration…` / `dev test tests/qualification…` → `dev test --lane=unit` or `dev check`
- `dev test integration` / `dev test integration_<suffix>` / `dev test pubsub` / `dev test qualification` → `dev test --lane=unit` or `dev check`
- Env- or `env`/`command`-wrapped variants of any of the above (`FOO=bar dev verify`, `env FOO=bar dev test --lane=qualification`, `command dev benchmark`) → the underlying `dev check`

**Removed or legacy `dev` flags** — bypass the current lane/cache/workflow surface:

- `dev test --force`, `dev test --no-testmon` → `dev test` or `dev test --rerun-failed`
- `dev test --full`, `dev test --available`, `dev test --integration`, `dev test --all` → `dev test --lane=unit` or `dev check`
- `dev test --lf` → `dev test --rerun-failed`
- `dev test --coverage`, `dev test -n …`, `dev test --num-workers …`, `dev test --timeout …` → `dev test --lane=unit` or `dev check` (lane owns these)
- `dev ci`, `dev list-tests` → `dev check` for validation, `dev info` for guidance

**Live or qualification `make` targets**:

- `make test-integration`, `make test-all` → `make test-fast` or `dev check`
- `make test EXTRA_ARGS=…tests/integration…|…tests/qualification…|…integration…|…qualification…` → `make test-fast` or `dev check`
- `make test tests/integration…` / `make test ./tests/qualification/…` (any positional `tests/integration` or `tests/qualification` path) → `make test-fast` or `dev check`

**Output piping that hangs or hides failures**:

- `pytest … | grep|head|tail|less|more` → `dev test` and read the captured log
- `dev <subcommand> … | grep|head|tail|less|more` → output is already in `.tmp/dev-runs/`; read the log

**Venv / non-system-wide installs** — the devcontainer uses system-wide tools:

- `uv sync`, `uv run …`, `uv venv`, `python -m venv`, `virtualenv` → the tools already on PATH

### What stays allowed

- `dev` subcommands not listed above: `dev`, `dev test`, `dev test --lane=unit`, `dev test --rerun-failed`, `dev test <module-or-alias>`, `dev lint`, `dev format`, `dev typecheck`, `dev check`, `dev status`, `dev info`, `dev examples --static`, `dev examples --smoke`.
- `make` targets other than `test-integration` / `test-all` / `test` invocations whose `EXTRA_ARGS` select live or qualification paths: e.g. `make test-fast`, `make check`.
- `uv pip install …`, `uvx …`.
- Read-only diagnostics: `pytest --version`, `pytest --help`, `ruff --version`, `interrogate`, `vulture`, `semgrep`.

## Installation

Part of the project's dev dependencies (uv workspace member):

```bash
uv pip install --system -e '.[dev]'
dev --help
```
