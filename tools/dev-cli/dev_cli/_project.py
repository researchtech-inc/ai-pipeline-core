"""Project-agnostic configuration via auto-detection + optional pyproject.toml overrides.

Replaces the old _config.py which had hardcoded project-specific constants.
Discovery order: auto-detect everything, then overlay [tool.dev-cli] from pyproject.toml.
"""

import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CheckStep:
    """A single CI check step (e.g., 'lint', 'typecheck')."""

    name: str
    description: str
    commands: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    """Fully resolved project configuration. Immutable after creation."""

    repo_root: Path
    runs_dir: Path
    state_file: Path

    source_to_test: dict[str, list[str]]
    scope_aliases: dict[str, str]
    markers: dict[str, str]
    test_groups: dict[str, str]
    checks: tuple[CheckStep, ...]
    tracked_extensions: frozenset[str]
    runner_prefix: tuple[str, ...]
    test_roots: list[str]
    infrastructure: tuple  # tuple[InfraCheck, ...]
    source_packages: list[str]
    coverage_sources: list[str]
    coverage_fail_under: int | None

    has_ruff: bool
    has_basedpyright: bool
    has_pyright: bool
    has_mypy: bool
    has_vulture: bool
    has_interrogate: bool
    has_semgrep: bool
    has_make: bool

    def command(self, executable: str, *args: str) -> tuple[str, ...]:
        """Build a command tuple using the detected runner prefix."""
        return (*self.runner_prefix, executable, *args)


# ---------------------------------------------------------------------------
# Root detection
# ---------------------------------------------------------------------------

_ROOT_MARKERS = ("pyproject.toml", ".git", "setup.cfg", "setup.py", "tox.ini")


def find_repo_root(start: Path | None = None) -> Path:
    """Walk up from `start` (default: cwd) to find the project root."""
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        for marker in _ROOT_MARKERS:
            if (directory / marker).exists():
                return directory
    return current


# ---------------------------------------------------------------------------
# Tool detection
# ---------------------------------------------------------------------------


def _has_tool(name: str) -> bool:
    return shutil.which(name) is not None


def _detect_runner_prefix(root: Path, cli_config: dict | None = None) -> tuple[str, ...]:
    """Detect how to run Python tools (bare, uv run, or poetry run).

    Priority:
    1. Explicit config: [tool.dev-cli] runner = "none" | "uv" | "poetry"
    2. If pytest is directly on PATH → bare (system install, devcontainer)
    3. If uv.lock exists → uv run
    4. If poetry.lock exists → poetry run
    5. Bare (fallback)
    """
    if cli_config:
        explicit = cli_config.get("runner")
        if explicit == "none":
            return ()
        if explicit == "uv":
            return ("uv", "run")
        if explicit == "poetry":
            return ("poetry", "run")

    # If pytest is already on PATH, tools are system-installed — no prefix needed
    if _has_tool("pytest"):
        return ()

    if (root / "uv.lock").exists() and _has_tool("uv"):
        return ("uv", "run")
    if (root / "poetry.lock").exists() and _has_tool("poetry"):
        return ("poetry", "run")
    return ()


# ---------------------------------------------------------------------------
# pyproject.toml parsing
# ---------------------------------------------------------------------------


def _read_pyproject(root: Path) -> dict:
    path = root / "pyproject.toml"
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def _read_dev_cli_config(pyproject: dict) -> dict:
    return pyproject.get("tool", {}).get("dev-cli", {})


def _read_pytest_config(pyproject: dict) -> dict:
    return pyproject.get("tool", {}).get("pytest", {}).get("ini_options", {})


def _parse_markers(pytest_config: dict) -> dict[str, str]:
    """Parse pytest markers into {name: description}."""
    result: dict[str, str] = {}
    for entry in pytest_config.get("markers", []):
        if ":" in entry:
            name, desc = entry.split(":", 1)
            result[name.strip()] = desc.strip()
        else:
            result[entry.strip()] = ""
    return result


def _read_testpaths(pytest_config: dict) -> list[str]:
    return pytest_config.get("testpaths", ["tests"])


def _extract_marker_from_addopts(addopts: str) -> str:
    """Extract -m '...' expression from an addopts string.

    Handles both `-m 'expr'` (single token) and `-m 'multi word expr'` (quoted spans).
    """
    import shlex

    try:
        tokens = shlex.split(addopts)
    except ValueError:
        tokens = addopts.split()
    for i, token in enumerate(tokens):
        if token == "-m" and i + 1 < len(tokens):
            return tokens[i + 1]
    return ""


def _addopts_without_markers(addopts: str) -> str:
    """Return addopts with -m expressions stripped."""
    import shlex

    try:
        tokens = shlex.split(addopts)
    except ValueError:
        tokens = addopts.split()
    result: list[str] = []
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token == "-m":
            skip_next = True
            continue
        result.append(token)
    return " ".join(result)


# ---------------------------------------------------------------------------
# Source & test directory discovery
# ---------------------------------------------------------------------------

_EXCLUDED_TOP_DIRS = frozenset({
    "tests",
    "test",
    "docs",
    "doc",
    "build",
    "dist",
    ".venv",
    "venv",
    "env",
    ".git",
    ".tmp",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "scripts",
    "examples",
    "tools",
    ".github",
})


def _discover_source_packages(root: Path) -> list[str]:
    """Find Python source packages at the top level or under src/."""
    candidates: list[str] = []

    src = root / "src"
    if src.is_dir():
        for child in sorted(src.iterdir()):
            if child.is_dir() and (child / "__init__.py").exists():
                candidates.append(str(child.relative_to(root)))

    for child in sorted(root.iterdir()):
        if child.is_dir() and child.name not in _EXCLUDED_TOP_DIRS and not child.name.startswith(".") and (child / "__init__.py").exists():
            candidates.append(child.name)

    return candidates


def _discover_test_subdirs(root: Path, test_roots: list[str]) -> list[str]:
    """Find all test subdirectories under each test root."""
    result: list[str] = []
    for test_root in test_roots:
        test_path = root / test_root
        if not test_path.is_dir():
            continue
        for child in sorted(test_path.iterdir()):
            if child.is_dir() and not child.name.startswith(".") and child.name not in ("__pycache__", "test_data"):
                result.append(str(child.relative_to(root)))
    return result


def _build_source_to_test_map(
    root: Path,
    source_packages: list[str],
    test_roots: list[str],
) -> dict[str, list[str]]:
    """Build source→test mapping by scanning actual imports in test files.

    Parses every test .py file with ast, finds imports from source packages,
    and maps each source submodule to the test directories that depend on it.
    Falls back to directory name matching if import scanning finds nothing.
    """
    from dev_cli._imports import scan_test_imports

    mapping = scan_test_imports(root, test_roots, source_packages)
    if mapping:
        return mapping

    # Fallback: name matching (for projects where import scanning finds nothing)
    test_subdirs = _discover_test_subdirs(root, test_roots)
    test_by_leaf: dict[str, list[str]] = {}
    for td in test_subdirs:
        leaf = Path(td).name
        test_by_leaf.setdefault(leaf, []).append(td)

    fallback: dict[str, list[str]] = {}
    for pkg in source_packages:
        pkg_path = root / pkg
        if not pkg_path.is_dir():
            continue
        for child in sorted(pkg_path.iterdir()):
            if not child.is_dir() or child.name == "__pycache__":
                continue
            source_key = str(child.relative_to(root))
            matched = test_by_leaf.get(child.name, [])
            if not matched and child.name.startswith("_"):
                matched = test_by_leaf.get(child.name.lstrip("_"), [])
            if matched:
                fallback[source_key] = list(matched)

    return fallback


def _build_scope_aliases(test_subdirs: list[str]) -> dict[str, str]:
    """Generate scope aliases from test subdirectory names."""
    aliases: dict[str, str] = {}
    for td in test_subdirs:
        leaf = Path(td).name
        if not leaf.startswith("_"):
            aliases[leaf] = td
    return aliases


# ---------------------------------------------------------------------------
# Test groups
# ---------------------------------------------------------------------------

_HEAVY_MARKER_NAMES = frozenset({
    "integration",
    "clickhouse",
    "pubsub",
    "pubsub_live",
    "slow",
    "e2e",
})


def _build_test_groups(markers: dict[str, str], pytest_config: dict) -> dict[str, str]:
    """Build test group marker expressions from project markers."""
    addopts = pytest_config.get("addopts", "")
    addopts_marker = _extract_marker_from_addopts(addopts)

    heavy_in_project = sorted(m for m in markers if m in _HEAVY_MARKER_NAMES)

    if addopts_marker:
        unit_expr = addopts_marker
    elif heavy_in_project:
        unit_expr = " and ".join(f"not {m}" for m in heavy_in_project)
    else:
        unit_expr = ""

    groups: dict[str, str] = {"unit": unit_expr, "all": ""}
    for m in heavy_in_project:
        groups[m] = m
    return groups


# ---------------------------------------------------------------------------
# CI check steps — auto-detect or read from config
# ---------------------------------------------------------------------------


def _build_check_steps(cfg_partial: _PartialConfig, root: Path, source_packages: list[str]) -> tuple[CheckStep, ...]:
    """Build CI check steps from detected tools. Fast checks first, slow last."""
    steps: list[CheckStep] = []
    r = cfg_partial.runner_prefix

    if cfg_partial.has_ruff:
        steps.append(
            CheckStep(
                "lint",
                "Ruff check + format",
                (
                    (*r, "ruff", "check", "."),
                    (*r, "ruff", "format", "--check", "."),
                ),
            )
        )

    if cfg_partial.has_basedpyright:
        cmds: list[tuple[str, ...]] = [(*r, "basedpyright", "--level", "warning")]
        if (root / "pyrightconfig.tests.json").exists():
            cmds.append((*r, "basedpyright", "--level", "error", "-p", "pyrightconfig.tests.json"))
        steps.append(CheckStep("typecheck", "BasedPyright", tuple(cmds)))
    elif cfg_partial.has_pyright:
        steps.append(CheckStep("typecheck", "Pyright", ((*r, "pyright"),)))
    elif cfg_partial.has_mypy:
        steps.append(CheckStep("typecheck", "Mypy", ((*r, "mypy", "."),)))

    if cfg_partial.has_vulture:
        vulture_cmd = [*r, "vulture"]
        # Auto-detect source packages and whitelist
        for child in sorted(root.iterdir()):
            if child.is_dir() and (child / "__init__.py").exists() and child.name not in _EXCLUDED_TOP_DIRS:
                vulture_cmd.append(f"{child.name}/")
        if (root / ".vulture_whitelist.py").exists():
            vulture_cmd.append(".vulture_whitelist.py")
        vulture_cmd.extend(["--min-confidence", "80"])
        steps.append(CheckStep("deadcode", "Vulture dead code", (tuple(vulture_cmd),)))

    if (root / ".semgrep").is_dir():
        semgrep_cmd = (*r, "semgrep", "--config", ".semgrep/", ".", "--error", "--quiet")
        if not cfg_partial.has_semgrep:
            semgrep_cmd = ("uvx", "semgrep", "--config", ".semgrep/", ".", "--error", "--quiet")
        steps.append(CheckStep("semgrep", "Semgrep custom rules", (semgrep_cmd,)))

    steps.append(CheckStep("test-validator", "Test authoring AST validator", ((*r, "python", "-m", "dev_cli._test_validator"),)))

    if cfg_partial.has_interrogate and source_packages:
        # Target source packages, not the whole repo (avoids scanning tests/tools)
        interrogate_targets = [pkg for pkg in source_packages if not pkg.startswith("src/")]
        if not interrogate_targets:
            interrogate_targets = source_packages
        steps.append(CheckStep("docstrings", "Docstring coverage", ((*r, "interrogate", "-v", "--fail-under", "100", *interrogate_targets),)))

    steps.append(CheckStep("test", "Unit tests", ((*r, "dev", "test", "--lane=unit"),)))

    return tuple(steps)


def _build_check_steps_from_config(cli_config: dict, runner: tuple[str, ...]) -> tuple[CheckStep, ...] | None:
    """Build check steps from explicit [tool.dev-cli.checks] config."""
    checks_list = cli_config.get("checks")
    if not checks_list:
        return None

    steps: list[CheckStep] = []
    for entry in checks_list:
        name = entry.get("name", "")
        desc = entry.get("description", name)
        run_entries = entry.get("run", [])
        commands: list[tuple[str, ...]] = []
        for cmd in run_entries:
            if isinstance(cmd, str):
                commands.append((*runner, *cmd.split()))
            elif isinstance(cmd, list):
                commands.append(tuple(cmd))
        if name and commands:
            steps.append(CheckStep(name=name, description=desc, commands=tuple(commands)))

    return tuple(steps) if steps else None


# ---------------------------------------------------------------------------
# Internal helper for partial config during construction
# ---------------------------------------------------------------------------


@dataclass
class _PartialConfig:
    runner_prefix: tuple[str, ...]
    has_ruff: bool
    has_basedpyright: bool
    has_pyright: bool
    has_mypy: bool
    has_vulture: bool
    has_interrogate: bool
    has_semgrep: bool
    has_make: bool


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

_cached_config: ProjectConfig | None = None


def load_config(*, force_reload: bool = False) -> ProjectConfig:
    """Load and cache project configuration.

    Auto-detects everything, then overlays [tool.dev-cli] from pyproject.toml.
    """
    global _cached_config
    if _cached_config is not None and not force_reload:
        return _cached_config

    root = find_repo_root()
    pyproject = _read_pyproject(root)
    cli_config = _read_dev_cli_config(pyproject)
    pytest_config = _read_pytest_config(pyproject)

    # Paths
    cache_dir = cli_config.get("cache_dir", ".tmp/dev-runs")
    runs_dir = root / cache_dir
    state_file = runs_dir / ".state.json"

    # Runner
    runner = _detect_runner_prefix(root, cli_config)

    # Tools
    has_ruff = _has_tool("ruff")
    has_basedpyright = _has_tool("basedpyright")
    has_pyright = _has_tool("pyright")
    has_mypy = _has_tool("mypy")
    has_vulture = _has_tool("vulture")
    has_interrogate = _has_tool("interrogate")
    has_semgrep = _has_tool("semgrep")
    has_make = _has_tool("make")

    partial = _PartialConfig(
        runner_prefix=runner,
        has_ruff=has_ruff,
        has_basedpyright=has_basedpyright,
        has_pyright=has_pyright,
        has_mypy=has_mypy,
        has_vulture=has_vulture,
        has_interrogate=has_interrogate,
        has_semgrep=has_semgrep,
        has_make=has_make,
    )

    # Markers + test paths
    markers = _parse_markers(pytest_config)
    test_roots = cli_config.get("test_roots") or _read_testpaths(pytest_config)
    test_subdirs = _discover_test_subdirs(root, test_roots)
    source_packages = _discover_source_packages(root)

    # Source-to-test mapping: auto-detect from imports + explicit overrides
    source_to_test = _build_source_to_test_map(root, source_packages, test_roots)
    for src_dir, test_dirs in cli_config.get("source_to_test", {}).items():
        source_to_test[src_dir] = list(test_dirs)

    # Scope aliases: auto from test subdirs + explicit overrides
    scope_aliases = _build_scope_aliases(test_subdirs)
    scope_aliases.update(cli_config.get("scopes", {}))

    # Test groups
    explicit_groups = cli_config.get("test_groups")
    test_groups = dict(explicit_groups) if explicit_groups else _build_test_groups(markers, pytest_config)

    # Check steps: explicit config overrides auto-detect
    checks = _build_check_steps_from_config(cli_config, runner)
    if checks is None:
        checks = _build_check_steps(partial, root, source_packages)

    # Tracked extensions
    tracked = frozenset(cli_config.get("tracked_extensions", [".py"]))

    # Infrastructure detection
    from dev_cli._infra import parse_infrastructure

    infrastructure = parse_infrastructure(cli_config)

    # Coverage config from [tool.coverage.*]
    cov_config = pyproject.get("tool", {}).get("coverage", {})
    coverage_sources = cov_config.get("run", {}).get("source", source_packages)
    coverage_fail_under_raw = cov_config.get("report", {}).get("fail_under")
    coverage_fail_under = int(coverage_fail_under_raw) if coverage_fail_under_raw is not None else None

    config = ProjectConfig(
        repo_root=root,
        runs_dir=runs_dir,
        state_file=state_file,
        source_to_test=source_to_test,
        scope_aliases=scope_aliases,
        markers=markers,
        test_groups=test_groups,
        checks=checks,
        tracked_extensions=tracked,
        runner_prefix=runner,
        test_roots=test_roots,
        infrastructure=infrastructure,
        source_packages=source_packages,
        coverage_sources=coverage_sources,
        coverage_fail_under=coverage_fail_under,
        has_ruff=has_ruff,
        has_basedpyright=has_basedpyright,
        has_pyright=has_pyright,
        has_mypy=has_mypy,
        has_vulture=has_vulture,
        has_interrogate=has_interrogate,
        has_semgrep=has_semgrep,
        has_make=has_make,
    )

    _cached_config = config
    return config
