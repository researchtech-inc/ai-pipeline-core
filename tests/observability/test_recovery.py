"""Tests for orphaned span reconciliation."""

import json
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from ai_pipeline_core.database import SpanKind, SpanRecord, SpanStatus
from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.observability._recovery import recover_orphaned_spans
from ai_pipeline_core.settings import Settings, settings


def _make_span(**kwargs: object) -> SpanRecord:
    deployment_id = kwargs.pop("deployment_id", uuid4())
    root_deployment_id = kwargs.pop("root_deployment_id", deployment_id)
    started_at: datetime = kwargs.pop("started_at", datetime(2026, 3, 14, 12, 0, tzinfo=UTC))
    defaults: dict[str, object] = {
        "span_id": kwargs.pop("span_id", uuid4()),
        "parent_span_id": kwargs.pop("parent_span_id", None),
        "deployment_id": deployment_id,
        "root_deployment_id": root_deployment_id,
        "run_id": kwargs.pop("run_id", "test-run"),
        "deployment_name": kwargs.pop("deployment_name", "test-deploy"),
        "kind": kwargs.pop("kind", SpanKind.TASK),
        "name": kwargs.pop("name", "TestTask"),
        "status": kwargs.pop("status", SpanStatus.RUNNING),
        "sequence_no": kwargs.pop("sequence_no", 0),
        "started_at": started_at,
        "ended_at": kwargs.pop("ended_at", None),
        "version": kwargs.pop("version", 1),
        "error_type": kwargs.pop("error_type", ""),
        "error_message": kwargs.pop("error_message", ""),
        "meta_json": kwargs.pop("meta_json", ""),
        "metrics_json": kwargs.pop("metrics_json", ""),
    }
    defaults.update(kwargs)
    return SpanRecord(**defaults)


class _FakePrefectClient:
    def __init__(self, behavior: object) -> None:
        self._behavior = behavior

    async def read_flow_run(self, _flow_run_id: UUID) -> object:
        if isinstance(self._behavior, Exception):
            raise self._behavior
        if callable(self._behavior):
            return self._behavior()
        return self._behavior


class _BrokenDatabase(_MemoryDatabase):
    async def latest_span_activity_for_deployment(self, root_deployment_id: UUID) -> datetime | None:
        _ = root_deployment_id
        raise TypeError("broken activity reader")


def _prefect_flow_run(*, state_type: str, updated: datetime | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        state_type=state_type,
        state=SimpleNamespace(type=state_type),
        updated=updated or datetime.now(UTC),
    )


def _prefect_meta(prefect_flow_run_id: UUID | None) -> str:
    if prefect_flow_run_id is None:
        return ""
    return json.dumps({"prefect_flow_run_id": str(prefect_flow_run_id)})


async def _seed_running_root(
    database: _MemoryDatabase,
    *,
    started_at: datetime,
    prefect_flow_run_id: UUID | None,
) -> SpanRecord:
    root_id = uuid4()
    root = _make_span(
        span_id=root_id,
        deployment_id=root_id,
        root_deployment_id=root_id,
        kind=SpanKind.DEPLOYMENT,
        name="Deploy",
        started_at=started_at,
        meta_json=_prefect_meta(prefect_flow_run_id),
    )
    await database.insert_span(root)
    return root


async def _add_task_child(
    database: _MemoryDatabase,
    *,
    root: SpanRecord,
    status: SpanStatus,
    ended_at: datetime | None = None,
    error_type: str = "",
    error_message: str = "",
) -> SpanRecord:
    child = _make_span(
        parent_span_id=root.span_id,
        deployment_id=root.deployment_id,
        root_deployment_id=root.root_deployment_id,
        kind=SpanKind.TASK,
        name="ChildTask",
        started_at=root.started_at + timedelta(seconds=1),
        status=status,
        ended_at=ended_at,
        error_type=error_type,
        error_message=error_message,
    )
    await database.insert_span(child)
    return child


@pytest.mark.asyncio
async def test_refuses_to_run_without_prefect_client_by_default() -> None:
    database = _MemoryDatabase()

    with pytest.raises(RuntimeError, match="refuses to run without a Prefect client"):
        await recover_orphaned_spans(database, prefect_client=None)


@pytest.mark.asyncio
async def test_running_prefect_state_never_reaps_regardless_of_activity_age() -> None:
    database = _MemoryDatabase()
    now = datetime.now(UTC)
    root = await _seed_running_root(
        database,
        started_at=now - timedelta(days=7),
        prefect_flow_run_id=uuid4(),
    )
    await _add_task_child(
        database,
        root=root,
        status=SpanStatus.RUNNING,
        ended_at=now - timedelta(days=6),
    )

    recovered = await recover_orphaned_spans(
        database,
        prefect_client=_FakePrefectClient(_prefect_flow_run(state_type="RUNNING", updated=now - timedelta(days=5))),
    )

    updated_root = await database.get_span(root.span_id)
    assert recovered == []
    assert updated_root is not None
    assert updated_root.status == SpanStatus.RUNNING


@pytest.mark.asyncio
async def test_completed_prefect_state_does_not_auto_reconcile(caplog: pytest.LogCaptureFixture) -> None:
    """B1: Prefect COMPLETED does not fabricate success — durable spans stay RUNNING."""
    database = _MemoryDatabase()
    now = datetime.now(UTC)
    root = await _seed_running_root(
        database,
        started_at=now - timedelta(hours=4),
        prefect_flow_run_id=uuid4(),
    )
    child = await _add_task_child(database, root=root, status=SpanStatus.RUNNING)
    caplog.set_level(logging.WARNING)

    recovered = await recover_orphaned_spans(
        database,
        prefect_client=_FakePrefectClient(_prefect_flow_run(state_type="COMPLETED", updated=now)),
    )

    updated_root = await database.get_span(root.span_id)
    updated_child = await database.get_span(child.span_id)
    assert recovered == []
    assert updated_root is not None
    assert updated_root.status == SpanStatus.RUNNING
    assert updated_child is not None
    assert updated_child.status == SpanStatus.RUNNING
    assert "Prefect reports COMPLETED" in caplog.text
    assert "durable spans are still RUNNING" in caplog.text


@pytest.mark.asyncio
async def test_completed_prefect_state_leaves_failed_children_untouched() -> None:
    """B1: Prefect COMPLETED with already-failed children leaves entire tree untouched."""
    database = _MemoryDatabase()
    now = datetime.now(UTC)
    root = await _seed_running_root(
        database,
        started_at=now - timedelta(hours=3),
        prefect_flow_run_id=uuid4(),
    )
    failed_child = await _add_task_child(
        database,
        root=root,
        status=SpanStatus.FAILED,
        ended_at=now - timedelta(hours=2),
        error_type="ProcessCrashed",
        error_message="already failed",
    )

    await recover_orphaned_spans(
        database,
        prefect_client=_FakePrefectClient(_prefect_flow_run(state_type="COMPLETED", updated=now)),
    )

    updated_root = await database.get_span(root.span_id)
    updated_child = await database.get_span(failed_child.span_id)
    assert updated_root is not None
    assert updated_root.status == SpanStatus.RUNNING
    assert updated_child is not None
    assert updated_child.status == SpanStatus.FAILED
    assert updated_child.error_type == "ProcessCrashed"


@pytest.mark.asyncio
async def test_terminal_failed_prefect_state_marks_tree_failed(caplog: pytest.LogCaptureFixture) -> None:
    database = _MemoryDatabase()
    now = datetime.now(UTC)
    root = await _seed_running_root(
        database,
        started_at=now - timedelta(hours=2),
        prefect_flow_run_id=uuid4(),
    )
    child = await _add_task_child(database, root=root, status=SpanStatus.RUNNING)
    caplog.set_level(logging.ERROR)

    recovered = await recover_orphaned_spans(
        database,
        prefect_client=_FakePrefectClient(_prefect_flow_run(state_type="FAILED", updated=now)),
    )

    updated_root = await database.get_span(root.span_id)
    updated_child = await database.get_span(child.span_id)
    matching = [record for record in caplog.records if record.message == "reconcile.orphaned_run"]
    assert recovered == [root.span_id]
    assert updated_root is not None
    assert updated_root.status == SpanStatus.FAILED
    assert updated_root.error_type == "ProcessCrashed"
    assert updated_child is not None
    assert updated_child.status == SpanStatus.FAILED
    assert matching
    assert matching[0].reap_reason == "prefect_state=FAILED"


@pytest.mark.asyncio
async def test_object_not_found_leaves_span_untouched_and_logs_actionable_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _FakeObjectNotFound(Exception):
        pass

    monkeypatch.setattr("ai_pipeline_core.observability._recovery.ObjectNotFound", _FakeObjectNotFound)
    database = _MemoryDatabase()
    now = datetime.now(UTC)
    root = await _seed_running_root(
        database,
        started_at=now - timedelta(days=3),
        prefect_flow_run_id=uuid4(),
    )
    caplog.set_level(logging.WARNING)

    recovered = await recover_orphaned_spans(
        database,
        prefect_client=_FakePrefectClient(_FakeObjectNotFound("missing")),
    )

    updated_root = await database.get_span(root.span_id)
    assert recovered == []
    assert updated_root is not None
    assert updated_root.status == SpanStatus.RUNNING
    assert f"Reconciler found root span `{root.span_id}` whose Prefect flow run " in caplog.text
    assert "Leaving the span untouched." in caplog.text
    assert "ai-trace recover --i-accept-nondeterministic-reconciliation" in caplog.text


@pytest.mark.asyncio
async def test_recovery_propagates_typeerror_not_swallow() -> None:
    database = _BrokenDatabase()
    root = await _seed_running_root(
        database,
        started_at=datetime.now(UTC) - timedelta(hours=1),
        prefect_flow_run_id=uuid4(),
    )
    await _add_task_child(database, root=root, status=SpanStatus.RUNNING)

    with pytest.raises(TypeError, match="broken activity reader"):
        await recover_orphaned_spans(
            database,
            prefect_client=_FakePrefectClient(_prefect_flow_run(state_type="FAILED")),
        )


def test_deleted_settings_are_actually_gone() -> None:
    assert "orphan_reap_heartbeat_stale_seconds" not in Settings.model_fields
    assert "orphan_reap_fallback_max_hours" not in Settings.model_fields
    assert not hasattr(settings, "orphan_reap_heartbeat_stale_seconds")
    assert not hasattr(settings, "orphan_reap_fallback_max_hours")


@pytest.mark.asyncio
async def test_env_var_alone_does_not_bypass_require_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """M1: Setting ORPHAN_REAP_REQUIRE_PREFECT_CLIENT=false alone is not enough."""
    monkeypatch.setattr(
        "ai_pipeline_core.observability._recovery.settings",
        Settings(orphan_reap_require_prefect_client=False),
    )
    database = _MemoryDatabase()

    with pytest.raises(RuntimeError, match="refuses to run without a Prefect client"):
        await recover_orphaned_spans(database, prefect_client=None)


@pytest.mark.asyncio
async def test_kwarg_alone_does_not_bypass_require_client() -> None:
    """M1: Passing require_prefect_client=False alone is not enough when env var is default."""
    database = _MemoryDatabase()

    with pytest.raises(RuntimeError, match="refuses to run without a Prefect client"):
        await recover_orphaned_spans(database, prefect_client=None, require_prefect_client=False)


@pytest.mark.asyncio
async def test_two_factor_unlock_allows_wall_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """M1: Both env var=False AND kwarg=False together allow wall-clock reconciliation."""
    monkeypatch.setattr(
        "ai_pipeline_core.observability._recovery.settings",
        Settings(orphan_reap_require_prefect_client=False),
    )
    database = _MemoryDatabase()
    now = datetime.now(UTC)
    root = await _seed_running_root(
        database,
        started_at=now - timedelta(hours=100),
        prefect_flow_run_id=uuid4(),
    )

    recovered = await recover_orphaned_spans(
        database,
        prefect_client=None,
        require_prefect_client=False,
    )

    assert recovered == [root.span_id]
    updated_root = await database.get_span(root.span_id)
    assert updated_root is not None
    assert updated_root.status == SpanStatus.FAILED
