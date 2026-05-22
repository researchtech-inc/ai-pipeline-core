"""CLI usage tests for generic replay."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from ai_pipeline_core.database.filesystem._backend import FilesystemDatabase
from ai_pipeline_core.replay.cli import _infer_db_path, main
from tests.unit.replay.conftest import make_span
from tests.support.model_catalog import DEFAULT_TEST_MODEL


class _MockResult:
    def __init__(self, content: str = "LLM response") -> None:
        self.content = content
        self.usage = SimpleNamespace(total_tokens=150, model_dump=lambda: {"total_tokens": 150})
        self.cost = 0.003


async def _seed_span(database: FilesystemDatabase, span: object) -> None:
    await database.insert_span(span)


def _conversation_span(tmp_path: Path) -> tuple[FilesystemDatabase, object]:
    database = FilesystemDatabase(tmp_path / "bundle")
    span = make_span(
        kind="conversation",
        name="ReplayConversation",
        target="decoded_method:ai_pipeline_core.llm.conversation:Conversation.send",
        meta={"model": "gemini-3-flash", "purpose": "summary"},
        metrics={"time_taken_ms": 123},
    )
    import asyncio

    asyncio.run(_seed_span(database, span))
    return database, span


@pytest.mark.ai_docs
def test_main_show_from_db_displays_meta_and_metrics(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _database, span = _conversation_span(tmp_path)

    exit_code = main(["show", "--from-db", str(span.span_id), "--db-path", str(tmp_path / "bundle")])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "meta_json" in output
    assert "metrics_json" in output
    assert "gemini-3-flash" in output


@pytest.mark.ai_docs
def test_main_run_from_file_writes_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _database, span = _conversation_span(tmp_path)
    replay_file = tmp_path / "bundle" / "spans" / f"{span.span_id}.json"

    async def fake_execute(span_id: Any, *, source_db: Any, sink_db: Any = None) -> _MockResult:
        _ = (span_id, source_db, sink_db)
        return _MockResult()

    monkeypatch.setattr("ai_pipeline_core.replay.cli.execute_span", fake_execute)

    exit_code = main(["run", str(replay_file), "--db-path", str(tmp_path / "bundle")])

    assert exit_code == 0
    output_dir = replay_file.parent / f"{replay_file.stem}_replay"
    assert (output_dir / "output.yaml").exists()


@pytest.mark.ai_docs
def test_main_run_with_overrides_uses_experiment_span(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _database, span = _conversation_span(tmp_path)
    replay_file = tmp_path / "bundle" / "spans" / f"{span.span_id}.json"

    async def fail_execute(span_id: Any, *, source_db: Any, sink_db: Any = None) -> _MockResult:
        _ = (span_id, source_db, sink_db)
        raise AssertionError("run with overrides should use experiment_span")

    async def fake_experiment_span(
        span_id: Any,
        *,
        source_db: Any,
        sink_db: Any = None,
        overrides: Any = None,
    ) -> Any:
        _ = (source_db, sink_db)
        assert span_id == span.span_id
        assert overrides is not None
        assert overrides.model == DEFAULT_TEST_MODEL
        assert overrides.model_options == {"reasoning_effort": "low"}
        return SimpleNamespace(result=_MockResult("override response"))

    monkeypatch.setattr("ai_pipeline_core.replay.cli.execute_span", fail_execute)
    monkeypatch.setattr("ai_pipeline_core.replay.cli.experiment_span", fake_experiment_span)

    exit_code = main([
        "run",
        str(replay_file),
        "--db-path",
        str(tmp_path / "bundle"),
        "--model",
        "gemini-3-flash",
        "--set",
        "reasoning_effort=low",
    ])

    assert exit_code == 0
    output_dir = replay_file.parent / f"{replay_file.stem}_replay"
    assert (output_dir / "output.yaml").exists()


def test_main_run_with_force_deployment_id_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _database, span = _conversation_span(tmp_path)
    replay_file = tmp_path / "bundle" / "spans" / f"{span.span_id}.json"

    async def fail_execute(span_id: Any, *, source_db: Any, sink_db: Any = None) -> _MockResult:
        _ = (span_id, source_db, sink_db)
        raise AssertionError("run with force deployment override should use experiment_span")

    async def fake_experiment_span(
        span_id: Any,
        *,
        source_db: Any,
        sink_db: Any = None,
        overrides: Any = None,
    ) -> Any:
        _ = (source_db, sink_db)
        assert span_id == span.span_id
        assert overrides is not None
        assert overrides.force_deployment_id == "depl-123"
        return SimpleNamespace(result=_MockResult("forced deployment response"))

    monkeypatch.setattr("ai_pipeline_core.replay.cli.execute_span", fail_execute)
    monkeypatch.setattr("ai_pipeline_core.replay.cli.experiment_span", fake_experiment_span)

    exit_code = main([
        "run",
        str(replay_file),
        "--db-path",
        str(tmp_path / "bundle"),
        "--force-deployment-id",
        "depl-123",
    ])

    assert exit_code == 0


@pytest.mark.ai_docs
def test_main_batch_uses_find_and_experiment_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _database, span = _conversation_span(tmp_path)
    deployment_id = span.root_deployment_id

    async def fake_find(
        database: Any,
        deployment_id_arg: Any,
        *,
        kind: Any = None,
        purpose: Any = None,
        task_class: Any = None,
    ) -> list[Any]:
        _ = database
        assert deployment_id_arg == deployment_id
        assert kind == "conversation"
        assert purpose == "summary"
        assert task_class is None
        return [span.span_id]

    async def fake_batch(
        span_ids: Any,
        source_db: Any,
        *,
        overrides: Any = None,
        concurrency: int = 5,
        sink_db: Any = None,
    ) -> list[Any]:
        _ = (source_db, sink_db)
        assert span_ids == [span.span_id]
        assert overrides is not None
        assert overrides.model == DEFAULT_TEST_MODEL
        assert overrides.model_options == {"reasoning_effort": "low"}
        assert concurrency == 3
        return [
            SimpleNamespace(
                source_span_id=span.span_id,
                model_used="gemini-3-flash",
                duration_seconds=0.5,
                cost_usd=0.0,
            )
        ]

    monkeypatch.setattr("ai_pipeline_core.replay.cli.find_experiment_span_ids", fake_find)
    monkeypatch.setattr("ai_pipeline_core.replay.cli.experiment_batch", fake_batch)

    exit_code = main([
        "batch",
        "--from-deployment",
        str(deployment_id),
        "--db-path",
        str(tmp_path / "bundle"),
        "--kind",
        "conversation",
        "--purpose",
        "summary",
        "--model",
        "gemini-3-flash",
        "--set",
        "reasoning_effort=low",
        "--concurrency",
        "3",
    ])

    assert exit_code == 0
    assert "Ran 1 replay experiments" in capsys.readouterr().out


def test_main_batch_with_force_deployment_id_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _database, span = _conversation_span(tmp_path)
    deployment_id = span.root_deployment_id

    async def fake_find(
        database: Any,
        deployment_id_arg: Any,
        *,
        kind: Any = None,
        purpose: Any = None,
        task_class: Any = None,
    ) -> list[Any]:
        _ = (database, kind, purpose, task_class)
        assert deployment_id_arg == deployment_id
        return [span.span_id]

    async def fake_batch(
        span_ids: Any,
        source_db: Any,
        *,
        overrides: Any = None,
        concurrency: int = 5,
        sink_db: Any = None,
    ) -> list[Any]:
        _ = (source_db, concurrency, sink_db)
        assert span_ids == [span.span_id]
        assert overrides is not None
        assert overrides.force_deployment_id == "depl-456"
        return [
            SimpleNamespace(
                source_span_id=span.span_id,
                model_used="recorded-model",
                duration_seconds=0.5,
                cost_usd=0.0,
            )
        ]

    monkeypatch.setattr("ai_pipeline_core.replay.cli.find_experiment_span_ids", fake_find)
    monkeypatch.setattr("ai_pipeline_core.replay.cli.experiment_batch", fake_batch)

    exit_code = main([
        "batch",
        "--from-deployment",
        str(deployment_id),
        "--db-path",
        str(tmp_path / "bundle"),
        "--force-deployment-id",
        "depl-456",
    ])

    assert exit_code == 0
    assert "Ran 1 replay experiments" in capsys.readouterr().out


def test__infer_db_path_finds_span_snapshot_root(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "snapshot"
    (snapshot_root / "spans").mkdir(parents=True)
    replay_file = snapshot_root / "spans" / f"{uuid4()}.json"
    replay_file.write_text("{}", encoding="utf-8")

    assert _infer_db_path(replay_file) == snapshot_root
