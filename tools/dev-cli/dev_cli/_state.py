"""Idempotency detection via file content hashing."""

import contextlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import xxhash


@dataclass(frozen=True, slots=True)
class PreviousRun:
    command: str
    timestamp: str
    exit_code: int
    summary: str
    log_file: str


@dataclass(frozen=True, slots=True)
class StepCacheEntry:
    """Cached result for a cacheable dev check step."""

    input_hash: str
    tool_version: str
    result: str
    log_file: str
    timestamp: str


_EXTRA_FILE_GLOBS = (
    "examples/**/*.py",
    "examples/manifest.yml",
    "benchmarks/**/*.py",
    "tests/conftest.py",
    "tests/support/**/*.py",
    "tools/dev-cli/**/*.py",
    ".semgrep/*.yml",
    "pyrightconfig*.json",
)
_EXTRA_FILES = ("pyproject.toml", ".env", ".vulture_whitelist.py")
_LITELLM_CANDIDATES = (
    ".tmp/production-server-v2/services/litellm/litellm.yaml",
    "litellm.yaml",
)
_CHANGE_SUFFIXES = (".py", ".toml", ".yml", ".yaml", ".json", ".env")


def _read_state_file() -> dict:
    from dev_cli._project import load_config

    cfg = load_config()
    if not cfg.state_file.exists():
        return {}
    try:
        data = json.loads(cfg.state_file.read_text())
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _write_state_file(data: dict) -> None:
    from dev_cli._project import load_config

    cfg = load_config()
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(cfg.runs_dir), suffix=".state.tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, cfg.state_file)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def _update_file_hash(hasher: xxhash.xxh64, repo_root: Path, filepath: Path) -> None:
    try:
        rel = filepath.relative_to(repo_root).as_posix()
    except ValueError:
        rel = filepath.as_posix()
    try:
        data = filepath.read_bytes()
    except OSError:
        return
    hasher.update(rel.encode())
    hasher.update(b"\0")
    hasher.update(data)
    hasher.update(b"\0")


def _update_text_hash(hasher: xxhash.xxh64, label: str, value: str) -> None:
    hasher.update(label.encode())
    hasher.update(b"\0")
    hasher.update(value.encode())
    hasher.update(b"\0")


def _iter_path_files(path: Path, tracked_extensions: frozenset[str]) -> list[Path]:
    if path.is_file():
        return [path] if path.suffix in tracked_extensions or path.name == ".env" else []
    if not path.exists():
        return []
    return [filepath for filepath in sorted(path.rglob("*")) if filepath.is_file() and filepath.suffix in tracked_extensions]


def _uv_pip_list_digest(repo_root: Path) -> str:
    try:
        result = subprocess.run(
            ["uv", "pip", "list", "--format", "json"],
            capture_output=True,
            text=True,
            cwd=repo_root,
            timeout=20,
        )
    except OSError, subprocess.TimeoutExpired:
        return ""
    if result.returncode != 0:
        return ""
    return xxhash.xxh64(result.stdout.encode()).hexdigest()


def hash_tracked_files(*paths: str) -> str:
    """Hash tracked inputs under paths plus config, env fingerprint, tools, and Python version."""
    from dev_cli._project import load_config

    cfg = load_config()

    hasher = xxhash.xxh64()
    for path_text in sorted(set(paths)):
        path = cfg.repo_root / path_text
        for filepath in _iter_path_files(path, cfg.tracked_extensions):
            _update_file_hash(hasher, cfg.repo_root, filepath)

    for pattern in _EXTRA_FILE_GLOBS:
        for filepath in sorted(cfg.repo_root.glob(pattern)):
            if filepath.is_file():
                _update_file_hash(hasher, cfg.repo_root, filepath)

    for filename in _EXTRA_FILES:
        filepath = cfg.repo_root / filename
        if filepath.is_file():
            _update_file_hash(hasher, cfg.repo_root, filepath)

    for filename in _LITELLM_CANDIDATES:
        filepath = cfg.repo_root / filename
        if filepath.is_file():
            _update_file_hash(hasher, cfg.repo_root, filepath)

    _update_text_hash(hasher, "python", ".".join(str(part) for part in sys.version_info[:3]))
    _update_text_hash(hasher, "uv-pip-list", _uv_pip_list_digest(cfg.repo_root))
    return hasher.hexdigest()


def git_changed_files() -> list[str]:
    """Get files changed relative to HEAD (staged + unstaged + untracked)."""
    from dev_cli._project import load_config

    cfg = load_config()

    result = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        capture_output=True,
        text=True,
        cwd=cfg.repo_root,
    )
    files = result.stdout.strip().splitlines() if result.returncode == 0 else []

    result2 = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        capture_output=True,
        text=True,
        cwd=cfg.repo_root,
    )
    if result2.returncode == 0:
        files.extend(result2.stdout.strip().splitlines())

    return [f for f in files if any(f.endswith(ext) for ext in (*cfg.tracked_extensions, *_CHANGE_SUFFIXES)) or f == ".env"]


def load_previous_run(command_key: str) -> PreviousRun | None:
    """Load previous run state for a given command key."""
    try:
        data = _read_state_file()
        r = data.get("runs", {}).get(command_key)
        if r is None:
            return None
        return PreviousRun(
            command=r["command"],
            timestamp=r["timestamp"],
            exit_code=r["exit_code"],
            summary=r["summary"],
            log_file=r["log_file"],
        )
    except (json.JSONDecodeError, KeyError) as e:
        print(f"WARN  Ignoring corrupt state file: {e}", file=sys.stderr)
        return None


def save_run_state(command_key: str, command: str, exit_code: int, summary: str, log_file: str, file_hash: str) -> None:
    """Save run state for idempotency detection (atomic write)."""
    data = _read_state_file()
    runs = data.setdefault("runs", {})
    runs[command_key] = {
        "command": command,
        "timestamp": datetime.now(UTC).isoformat(),
        "exit_code": exit_code,
        "summary": summary,
        "log_file": log_file,
        "file_hash": file_hash,
    }
    data.setdefault("step_cache", {})
    _write_state_file(data)


def check_idempotency(command_key: str, file_hash: str) -> PreviousRun | None:
    """Check if a command was already run with the same file hash."""
    try:
        data = _read_state_file()
        r = data.get("runs", {}).get(command_key)
        if r is None:
            return None
        if r.get("file_hash") == file_hash:
            return PreviousRun(
                command=r["command"],
                timestamp=r["timestamp"],
                exit_code=r["exit_code"],
                summary=r["summary"],
                log_file=r["log_file"],
            )
    except (json.JSONDecodeError, KeyError) as e:
        print(f"WARN  Ignoring corrupt state file: {e}", file=sys.stderr)
    return None


def load_step_cache(step_name: str) -> StepCacheEntry | None:
    """Load a cached dev check step result."""
    data = _read_state_file()
    raw = data.get("step_cache", {}).get(step_name)
    if not isinstance(raw, dict):
        return None
    try:
        return StepCacheEntry(
            input_hash=raw["input_hash"],
            tool_version=raw["tool_version"],
            result=raw["result"],
            log_file=raw["log_file"],
            timestamp=raw["timestamp"],
        )
    except KeyError:
        return None


def save_step_cache(step_name: str, input_hash: str, tool_version: str, result: str, log_file: str) -> None:
    """Persist a cacheable dev check step result."""
    data = _read_state_file()
    step_cache = data.setdefault("step_cache", {})
    step_cache[step_name] = {
        "input_hash": input_hash,
        "tool_version": tool_version,
        "result": result,
        "log_file": log_file,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    data.setdefault("runs", {})
    _write_state_file(data)


def load_failure_dependency_hash(nodeid: str) -> str | None:
    """Return the dependency digest recorded when a nodeid last failed."""
    data = _read_state_file()
    raw = data.get("rerun_failed", {}).get(nodeid)
    if not isinstance(raw, dict):
        return None
    value = raw.get("dependency_hash")
    return value if isinstance(value, str) else None


def save_failure_dependency_hashes(items: dict[str, str]) -> None:
    """Persist dependency digests for failing nodeids."""
    if not items:
        return
    data = _read_state_file()
    rerun_failed = data.setdefault("rerun_failed", {})
    timestamp = datetime.now(UTC).isoformat()
    for nodeid, dependency_hash in items.items():
        rerun_failed[nodeid] = {
            "dependency_hash": dependency_hash,
            "timestamp": timestamp,
        }
    data.setdefault("runs", {})
    data.setdefault("step_cache", {})
    _write_state_file(data)
