"""Span-era tests for the ai-trace CLI."""

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar
from uuid import UUID, uuid4

import pytest

from ai_pipeline_core.database import LogRecord, SpanKind, SpanRecord, SpanStatus
from ai_pipeline_core.database.filesystem._backend import FilesystemDatabase
from ai_pipeline_core.observability import cli as trace_cli
from ai_pipeline_core.observability.cli import (
    _resolve_connection,
    _resolve_identifier,
    main,
)


def _make_span(**kwargs: object) -> SpanRecord:
    deployment_id = kwargs.pop("deployment_id", uuid4())
    root_deployment_id = kwargs.pop("root_deployment_id", deployment_id)
    started_at = kwargs.pop("started_at", datetime(2026, 3, 11, 12, 0, tzinfo=UTC))
    if not isinstance(started_at, datetime):
        msg = "started_at must be a datetime"
        raise TypeError(msg)
    defaults: dict[str, object] = {
        "span_id": kwargs.pop("span_id", uuid4()),
        "parent_span_id": None,
        "deployment_id": deployment_id,
        "root_deployment_id": root_deployment_id,
        "run_id": "span-run",
        "deployment_name": "span-cli-pipeline",
        "kind": SpanKind.TASK,
        "name": "ExampleTask",
        "description": "",
        "status": SpanStatus.COMPLETED,
        "sequence_no": 0,
        "started_at": started_at,
        "ended_at": started_at + timedelta(seconds=1),
        "version": 1,
        "cache_key": "",
        "previous_conversation_id": None,
        "cost_usd": 0.0,
        "error_type": "",
        "error_message": "",
        "input_document_shas": (),
        "output_document_shas": (),
        "target": "",
        "receiver_json": "",
        "input_json": "",
        "output_json": "",
        "error_json": "",
        "meta_json": "",
        "metrics_json": "",
        "input_blob_shas": (),
        "output_blob_shas": (),
    }
    defaults.update(kwargs)
    return SpanRecord(**defaults)


async def _seed_span_snapshot(base_path: Path) -> tuple[FilesystemDatabase, UUID, UUID]:
    database = FilesystemDatabase(base_path)
    deployment_id = uuid4()
    base = datetime(2026, 3, 11, 12, 0, tzinfo=UTC)

    deployment = _make_span(
        span_id=uuid4(),
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        kind=SpanKind.DEPLOYMENT,
        name="RootDeployment",
        deployment_name="span-cli-pipeline",
        run_id="span-run",
        started_at=base,
        ended_at=base + timedelta(seconds=20),
    )
    flow = _make_span(
        span_id=uuid4(),
        parent_span_id=deployment.span_id,
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        kind=SpanKind.FLOW,
        name="AnalyzeFlow",
        sequence_no=1,
        started_at=base + timedelta(seconds=1),
        ended_at=base + timedelta(seconds=18),
        meta_json=json.dumps({"step": 1, "total_steps": 1}),
    )
    task = _make_span(
        span_id=uuid4(),
        parent_span_id=flow.span_id,
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        kind=SpanKind.TASK,
        name="AnalyzeTask",
        sequence_no=1,
        started_at=base + timedelta(seconds=2),
        ended_at=base + timedelta(seconds=16),
    )
    operation = _make_span(
        span_id=uuid4(),
        parent_span_id=task.span_id,
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        kind=SpanKind.OPERATION,
        name="collect_context",
        sequence_no=1,
        started_at=base + timedelta(seconds=3),
        ended_at=base + timedelta(seconds=12),
    )
    conversation = _make_span(
        span_id=uuid4(),
        parent_span_id=operation.span_id,
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        kind=SpanKind.CONVERSATION,
        name="ConversationBoundary",
        sequence_no=1,
        started_at=base + timedelta(seconds=4),
        ended_at=base + timedelta(seconds=9),
        meta_json=json.dumps({
            "purpose": "analyze_document",
            "model": "gpt-5.4",
        }),
        metrics_json=json.dumps({
            "tokens_input": 2200,
            "tokens_output": 180,
            "tokens_cache_read": 1500,
            "tokens_reasoning": 25,
        }),
    )
    llm_round = _make_span(
        span_id=uuid4(),
        parent_span_id=conversation.span_id,
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        kind=SpanKind.LLM_ROUND,
        name="round-1",
        sequence_no=1,
        started_at=base + timedelta(seconds=4),
        ended_at=base + timedelta(seconds=6),
        cost_usd=0.42,
        meta_json=json.dumps({
            "round_index": 1,
            "model": "gpt-5.4",
        }),
        metrics_json=json.dumps({
            "tokens_input": 2200,
            "tokens_output": 180,
            "tokens_cache_read": 1500,
            "tokens_reasoning": 25,
        }),
    )
    tool_call = _make_span(
        span_id=uuid4(),
        parent_span_id=conversation.span_id,
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        kind=SpanKind.TOOL_CALL,
        name="tool-call",
        sequence_no=2,
        started_at=base + timedelta(seconds=6),
        ended_at=base + timedelta(seconds=7),
        meta_json=json.dumps({"round_index": 1, "tool_name": "web_search"}),
    )

    for span in (deployment, flow, task, operation, conversation, llm_round, tool_call):
        await database.insert_span(span)

    await database.save_logs_batch([
        LogRecord(
            deployment_id=deployment_id,
            span_id=task.span_id,
            timestamp=base + timedelta(seconds=2),
            sequence_no=1,
            level="INFO",
            category="framework",
            logger_name="ai_pipeline_core.tests",
            message="task started",
        ),
        LogRecord(
            deployment_id=deployment_id,
            span_id=conversation.span_id,
            timestamp=base + timedelta(seconds=9),
            sequence_no=2,
            level="INFO",
            category="llm",
            logger_name="ai_pipeline_core.tests",
            message="conversation finished",
            fields_json='{"rounds": 1}',
        ),
    ])

    return database, deployment_id, conversation.span_id


class TestSpanResolveConnection:
    def test_db_path_returns_span_filesystem_database(self, tmp_path: Path) -> None:
        FilesystemDatabase(tmp_path)
        args = type("Args", (), {"command": "show", "db_path": str(tmp_path)})()

        database = _resolve_connection(args)

        assert isinstance(database, FilesystemDatabase)
        assert database.read_only is True

    def test_recover_db_path_opens_writable_filesystem_database(self, tmp_path: Path) -> None:
        FilesystemDatabase(tmp_path)
        args = type("Args", (), {"command": "recover", "db_path": str(tmp_path)})()

        database = _resolve_connection(args)

        assert isinstance(database, FilesystemDatabase)
        assert database.read_only is False

    def test_live_connection_prefers_span_clickhouse(self, monkeypatch: pytest.MonkeyPatch) -> None:
        @dataclass(slots=True)
        class _Settings:
            clickhouse_host: ClassVar[str] = "clickhouse.local"

        @dataclass(slots=True)
        class _FakeClickHouseDatabase:
            settings: object

        monkeypatch.setattr(trace_cli, "settings", _Settings())
        monkeypatch.setattr(trace_cli, "ClickHouseDatabase", _FakeClickHouseDatabase)

        database = _resolve_connection(type("Args", (), {"command": "show", "db_path": None})())

        assert isinstance(database, _FakeClickHouseDatabase)
        assert isinstance(database.settings, _Settings)


class TestSpanResolveIdentifier:
    def test_span_uuid_resolves_root_deployment_id(self, tmp_path: Path) -> None:
        database, deployment_id, conversation_id = asyncio.run(_seed_span_snapshot(tmp_path))

        resolved = _resolve_identifier(str(conversation_id), database)

        assert resolved == (deployment_id, "span-run")


class TestSpanShowDeployment:
    def test_main_show_command_reads_span_snapshot(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        asyncio.run(_seed_span_snapshot(tmp_path))

        result = main(["show", "span-run", "--db-path", str(tmp_path)])

        assert result == 0
        output = capsys.readouterr().out
        assert "Deployment span-cli-pipeline / span-run" in output
        assert "conversation: analyze_document" in output
        assert "task started" in output
        assert '"rounds": 1' in output

    def test_main_download_command_writes_span_summary_artifacts(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        source_dir = tmp_path / "source"
        output_dir = tmp_path / "download"
        asyncio.run(_seed_span_snapshot(source_dir))

        result = main(["download", "span-run", "--db-path", str(source_dir), "--output-dir", str(output_dir)])

        assert result == 0
        assert "Downloaded deployment" in capsys.readouterr().out
        assert (output_dir / "logs.jsonl").exists()
