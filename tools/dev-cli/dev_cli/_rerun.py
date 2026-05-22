"""Filtered rerun support for previously failed pytest nodeids."""

import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RerunSet:
    """The nodeids selected for a rerun and whether pytest should be invoked."""

    nodeids: tuple[str, ...]
    should_run: bool


def compute_rerun_set(repo_root: Path, scope_dirs: tuple[str, ...]) -> RerunSet:
    """Read lastfailed and testmon data, then return the nodeids that still need a rerun."""
    recorded = _read_lastfailed(repo_root)
    lastfailed = tuple(nodeid for nodeid in recorded if _nodeid_in_scope(nodeid, scope_dirs))
    if not lastfailed:
        message = "no recorded failures" if not recorded else "no recorded failures in selected scope"
        print(f"SKIP rerun-failed — {message}")
        return RerunSet((), should_run=False)

    testmon_path = repo_root / ".testmondata"
    if not testmon_path.exists():
        print(f"NOTE rerun-failed: testmon data unavailable; rerunning all {len(lastfailed)} failed entries without filtering")
        return RerunSet(tuple(lastfailed), should_run=True)

    try:
        with sqlite3.connect(testmon_path) as conn:
            nodeids = _filter_with_testmon(conn, repo_root, lastfailed)
    except sqlite3.DatabaseError:
        print("WARN rerun-failed: .testmondata corrupt; falling back to unfiltered rerun. Consider 'dev test --lane=unit' to rebuild.", file=sys.stderr)
        return RerunSet(tuple(lastfailed), should_run=True)
    except sqlite3.OperationalError:
        print(f"NOTE rerun-failed: testmon data unavailable; rerunning all {len(lastfailed)} failed entries without filtering")
        return RerunSet(tuple(lastfailed), should_run=True)

    if not nodeids:
        print("SKIP rerun-failed — recorded failures are stale")
        return RerunSet((), should_run=False)
    return RerunSet(tuple(nodeids), should_run=True)


def _read_lastfailed(repo_root: Path) -> tuple[str, ...]:
    cache_path = repo_root / ".pytest_cache" / "v" / "cache" / "lastfailed"
    if not cache_path.exists():
        return ()
    try:
        raw = json.loads(cache_path.read_text())
    except (OSError, json.JSONDecodeError):  # fmt: skip
        return ()
    if not isinstance(raw, dict):
        return ()
    return tuple(nodeid for nodeid, failed in raw.items() if failed and _nodeid_file_exists(repo_root, nodeid))


def _nodeid_file_exists(repo_root: Path, nodeid: str) -> bool:
    path_text = nodeid.split("::", 1)[0]
    return (repo_root / path_text).exists()


def _nodeid_in_scope(nodeid: str, scope_dirs: tuple[str, ...]) -> bool:
    path_text = nodeid.split("::", 1)[0]
    return any(path_text == scope.rstrip("/") or path_text.startswith(f"{scope.rstrip('/')}/") for scope in scope_dirs)


def _filter_with_testmon(conn: sqlite3.Connection, repo_root: Path, nodeids: tuple[str, ...]) -> list[str]:
    from dev_cli._state import hash_tracked_files, load_failure_dependency_hash

    if not _has_testmon_schema(conn):
        print(f"NOTE rerun-failed: testmon data unavailable; rerunning all {len(nodeids)} failed entries without filtering")
        return list(nodeids)

    selected: list[str] = []
    for nodeid in nodeids:
        deps = _test_dependencies(conn, nodeid)
        if not deps:
            selected.append(nodeid)
            continue
        filenames = [filename for filename, _recorded_fsha in deps]
        previous_hash = load_failure_dependency_hash(nodeid)
        if previous_hash is None:
            selected.append(nodeid)
            continue
        if hash_tracked_files(*filenames) != previous_hash:
            selected.append(nodeid)
    return selected


def record_failure_dependency_hashes(repo_root: Path, nodeids: tuple[str, ...]) -> None:
    """Store current dependency digests for failed nodeids when testmon data is available."""
    if not nodeids:
        return
    testmon_path = repo_root / ".testmondata"
    if not testmon_path.exists():
        return
    try:
        with sqlite3.connect(testmon_path) as conn:
            if not _has_testmon_schema(conn):
                return
            _record_hashes_from_conn(conn, nodeids)
    except sqlite3.DatabaseError:
        return


def _has_testmon_schema(conn: sqlite3.Connection) -> bool:
    tables = {row[0] for row in conn.execute("select name from sqlite_master where type='table'")}
    return {"test_execution", "test_execution_file_fp", "file_fp"}.issubset(tables)


def _test_dependencies(conn: sqlite3.Connection, nodeid: str) -> list[tuple[str, str]]:
    rows = conn.execute(
        """
        select file_fp.filename, file_fp.fsha
        from test_execution
        join test_execution_file_fp on test_execution.id = test_execution_file_fp.test_execution_id
        join file_fp on file_fp.id = test_execution_file_fp.fingerprint_id
        where test_execution.test_name = ?
        order by test_execution.id desc
        """,
        (nodeid,),
    ).fetchall()
    return [(str(filename), str(fsha)) for filename, fsha in rows if filename and fsha]


def _record_hashes_from_conn(conn: sqlite3.Connection, nodeids: tuple[str, ...]) -> None:
    from dev_cli._state import hash_tracked_files, save_failure_dependency_hashes

    snapshots: dict[str, str] = {}
    for nodeid in nodeids:
        deps = _test_dependencies(conn, nodeid)
        if deps:
            snapshots[nodeid] = hash_tracked_files(*(filename for filename, _recorded_fsha in deps))
    save_failure_dependency_hashes(snapshots)
