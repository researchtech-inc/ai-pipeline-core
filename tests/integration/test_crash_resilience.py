"""Integration tests for crash resilience and orphan recovery."""

import signal
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from ai_pipeline_core.database import SpanKind, SpanRecord, SpanStatus
from ai_pipeline_core.database.filesystem._backend import FilesystemDatabase
from ai_pipeline_core.observability._recovery import recover_orphaned_spans
from ai_pipeline_core.settings import Settings


def _start_worker(tmp_path: Path, mode: str) -> tuple[subprocess.Popen[str], Path]:
    db_path = tmp_path / mode
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tests.integration.crash_worker",
            "--db-path",
            str(db_path),
            "--mode",
            mode,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    deadline = time.time() + 15
    while time.time() < deadline:
        if db_path.exists() and any(db_path.rglob("*.json")):
            break
        if process.poll() is not None:
            break
        time.sleep(0.2)
    return process, db_path


async def _load_root_spans(db_path: Path, *, read_only: bool = True) -> list[SpanRecord]:
    database = FilesystemDatabase(db_path, read_only=read_only)
    try:
        return await database.list_deployments(limit=10, root_only=True)
    finally:
        await database.shutdown()


class _FakePrefectClient:
    async def read_flow_run(self, _flow_run_id: UUID) -> SimpleNamespace:
        return SimpleNamespace(
            state_type="CRASHED",
            state=SimpleNamespace(type="CRASHED"),
            updated=datetime.now(UTC),
        )


@pytest.mark.available
@pytest.mark.asyncio
async def test_sigterm_preserves_terminal_span_state(tmp_path: Path) -> None:
    process, db_path = _start_worker(tmp_path, "sigterm-flow")
    time.sleep(2)
    process.send_signal(signal.SIGTERM)
    process.wait(timeout=10)

    deadline = time.time() + 5
    roots = await _load_root_spans(db_path)
    while time.time() < deadline and any(root.status == SpanStatus.RUNNING for root in roots):
        time.sleep(0.2)
        roots = await _load_root_spans(db_path)

    assert roots
    assert all(root.status != SpanStatus.RUNNING for root in roots)


@pytest.mark.available
@pytest.mark.asyncio
async def test_sigkill_leaves_orphan_then_recovery_reaps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    process, db_path = _start_worker(tmp_path, "sigkill-flow")
    process.kill()
    process.wait(timeout=10)

    roots_before = await _load_root_spans(db_path)
    assert roots_before
    assert any(root.status == SpanStatus.RUNNING for root in roots_before)

    monkeypatch.setattr(
        "ai_pipeline_core.observability._recovery.settings",
        Settings(orphan_reap_require_prefect_client=False),
    )
    database = FilesystemDatabase(db_path, read_only=False)
    try:
        recovered = await recover_orphaned_spans(
            database,
            prefect_client=None,
            require_prefect_client=False,
            fallback_max_hours=0,
        )
    finally:
        await database.shutdown()

    roots_after = await _load_root_spans(db_path)
    assert recovered
    assert all(root.status == SpanStatus.FAILED for root in roots_after)


@pytest.mark.available
@pytest.mark.asyncio
async def test_orphan_recovery_reaps_spans_with_fake_crashed_prefect_state(tmp_path: Path) -> None:
    process, db_path = _start_worker(tmp_path, "sigkill-flow")
    process.kill()
    process.wait(timeout=10)

    database = FilesystemDatabase(db_path, read_only=False)
    try:
        roots_before = await database.list_deployments(limit=10, root_only=True)
        assert roots_before
        await database.insert_span(
            replace(
                roots_before[0],
                version=roots_before[0].version + 1,
                meta_json='{"prefect_flow_run_id":"00000000-0000-0000-0000-000000000001"}',
            )
        )
        recovered = await recover_orphaned_spans(
            database,
            prefect_client=_FakePrefectClient(),
        )
        tree = await database.get_deployment_tree(recovered[0])
    finally:
        await database.shutdown()

    assert recovered
    assert tree
    assert any(span.kind == SpanKind.DEPLOYMENT for span in tree)
    assert all(span.status == SpanStatus.FAILED for span in tree)
    assert all(span.error_type == "ProcessCrashed" for span in tree)
