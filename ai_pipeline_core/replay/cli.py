"""CLI for generic replay and replay experiments."""

import argparse
import asyncio
import importlib
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml
from pydantic import BaseModel

from ai_pipeline_core._codec import import_by_path
from ai_pipeline_core._llm_core.types import AIModel
from ai_pipeline_core.database._factory import Database, create_database_from_settings
from ai_pipeline_core.database._json_helpers import parse_json_object
from ai_pipeline_core.database._protocol import DatabaseReader, DatabaseWriter
from ai_pipeline_core.database._types import SpanRecord
from ai_pipeline_core.database.filesystem._backend import FilesystemDatabase
from ai_pipeline_core.database.filesystem.overlay import create_debug_sink
from ai_pipeline_core.documents.document import Document
from ai_pipeline_core.settings import settings

from ._execute import execute_span
from ._experiment import ExperimentOverrides, experiment_batch, experiment_span, find_experiment_span_ids

__all__ = ["main"]

logger = logging.getLogger(__name__)
_CONTENT_PREVIEW_LENGTH = 200


def _infer_db_path(replay_file: Path) -> Path:
    """Walk up from a replay file to find a snapshot root."""
    current = replay_file.resolve().parent
    while current != current.parent:
        if (current / "spans").is_dir() or (current / "blobs").is_dir():
            return current
        current = current.parent
    raise FileNotFoundError(
        f"Could not find a snapshot root above {replay_file}. Use --db-path to point at the FilesystemDatabase root."
    )


def _import_modules(modules: list[str]) -> None:
    for module_name in modules:
        importlib.import_module(module_name)


async def _load_span_from_database(database: DatabaseReader, span_id: UUID) -> SpanRecord:
    span = await database.get_span(span_id)
    if span is None:
        raise FileNotFoundError(f"Span {span_id} was not found in the database.")
    return span


def _resolve_database_for_span(db_path: str | None) -> tuple[Database, str]:
    if db_path is not None:
        resolved_path = Path(db_path).resolve()
        return FilesystemDatabase(resolved_path, read_only=True), str(resolved_path)
    if not settings.clickhouse_host:
        raise ValueError(
            "--from-db without --db-path requires CLICKHOUSE_HOST, or pass --db-path to a FilesystemDatabase snapshot."
        )
    return create_database_from_settings(settings), "clickhouse"


def _resolve_database_for_file(replay_file: Path, db_path: str | None) -> tuple[FilesystemDatabase, str]:
    resolved_path = Path(db_path).resolve() if db_path else _infer_db_path(replay_file)
    return FilesystemDatabase(resolved_path, read_only=True), str(resolved_path)


def _validate_replay_file(replay_file: Path) -> None:
    if not replay_file.exists():
        raise FileNotFoundError(f"File not found: {replay_file}")
    if replay_file.is_dir():
        raise ValueError(
            f"Expected a span JSON file, got directory {replay_file}. "
            "Use --from-db <span_id> or point at one span JSON file."
        )
    if replay_file.suffix != ".json":
        raise ValueError(
            f"Replay file {replay_file} must be a span JSON file. YAML replay payloads are no longer supported."
        )


def _read_span_id_from_json_file(replay_file: Path) -> UUID:
    raw_value = json.loads(replay_file.read_text(encoding="utf-8"))
    if not isinstance(raw_value, dict):
        raise TypeError(f"Expected JSON object in {replay_file}, got {type(raw_value).__name__}.")
    span_id_raw = raw_value.get("span_id")
    if not isinstance(span_id_raw, str):
        raise TypeError(f"Replay file {replay_file} is missing string field 'span_id'.")
    return UUID(span_id_raw)


async def _load_span_from_file_path(replay_file: Path, database: DatabaseReader) -> SpanRecord:
    await asyncio.to_thread(_validate_replay_file, replay_file)
    try:
        span_id = UUID(replay_file.stem)
    except ValueError:
        span_id = await asyncio.to_thread(_read_span_id_from_json_file, replay_file)
    return await _load_span_from_database(database, span_id)


def _serialize_result(result: Any) -> dict[str, Any]:
    output: dict[str, Any] = {"timestamp": datetime.now(UTC).isoformat()}
    if hasattr(result, "content") and hasattr(result, "usage"):
        output["type"] = "conversation"
        output["content"] = result.content or ""
        output["cost"] = result.cost
        usage = result.usage
        output["usage"] = usage.model_dump() if isinstance(usage, BaseModel) else {"total_tokens": usage.total_tokens}
        return output
    if isinstance(result, Document):
        output["type"] = "document"
        output["name"] = result.name
        output["sha256"] = result.sha256
        return output
    if isinstance(result, tuple) and all(isinstance(item, Document) for item in result):
        output["type"] = "document_list"
        output["documents"] = [{"name": item.name, "sha256": item.sha256} for item in result]
        return output
    output["type"] = type(result).__name__
    output["value"] = repr(result)
    return output


def _format_result(result: Any) -> str:
    if hasattr(result, "content") and hasattr(result, "usage"):
        content: str = result.content or ""
        preview = content[:_CONTENT_PREVIEW_LENGTH]
        if len(content) > _CONTENT_PREVIEW_LENGTH:
            preview += "..."
        return preview
    if isinstance(result, Document):
        return f"{type(result).__name__}: {result.name}"
    if isinstance(result, tuple):
        doc_lines: list[str] = []
        for item in result:
            if not isinstance(item, Document):
                doc_lines.clear()
                break
            doc_lines.append(f"{type(item).__name__}: {item.name}")
        if doc_lines:
            return "\n".join(doc_lines)
    return str(result)


def _write_output(output_dir: Path, result: Any) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "output.yaml"
    output_path.write_text(
        yaml.dump(_serialize_result(result), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return output_path


def _default_output_dir(replay_file: Path | None, from_db: str | None) -> Path:
    if replay_file is not None:
        return replay_file.parent / f"{replay_file.stem}_replay"
    if from_db is not None:
        return Path.cwd() / f"span_{from_db[:8]}_replay"
    return Path.cwd() / "replay_output"


def _load_response_format(path: str) -> type[BaseModel]:
    loaded: Any = import_by_path(path)
    if not isinstance(loaded, type) or not issubclass(loaded, BaseModel):
        raise TypeError(f"Replay override response_format={path!r} did not resolve to a BaseModel subclass.")
    return loaded


def _build_overrides(args: argparse.Namespace) -> ExperimentOverrides | None:
    model_options: dict[str, Any] = {}
    override_values: dict[str, Any] = {}
    if args.model:
        override_values["model"] = AIModel(args.model)
    if getattr(args, "force_deployment_id", None):
        override_values["force_deployment_id"] = args.force_deployment_id
    set_items: list[str] = args.set or []
    for item in set_items:
        if "=" not in item:
            raise ValueError(f"Invalid --set value {item!r}. Use KEY=VALUE.")
        key, raw_value = item.split("=", 1)
        value: Any = yaml.safe_load(raw_value)
        if key == "model":
            override_values["model"] = AIModel.model_validate(value) if isinstance(value, dict) else AIModel(str(value))
            continue
        if key == "response_format":
            if not isinstance(value, str):
                raise TypeError("--set response_format=... requires an import path string.")
            override_values["response_format"] = _load_response_format(value)
            continue
        model_options[key] = value
    if model_options:
        override_values["model_options"] = model_options
    if not override_values:
        return None
    return ExperimentOverrides(**override_values)


def _print_show(span: SpanRecord) -> None:
    meta = parse_json_object(span.meta_json, context=f"Span {span.span_id}", field_name="meta_json")
    metrics = parse_json_object(span.metrics_json, context=f"Span {span.span_id}", field_name="metrics_json")
    print(f"Kind: {span.kind}")
    print(f"Span ID: {span.span_id}")
    print(f"Name: {span.name}")
    print(f"Status: {span.status}")
    print(f"Target: {span.target or '<non-replayable>'}")
    print("\nmeta_json:")
    print(json.dumps(meta, indent=2, sort_keys=True))
    print("\nmetrics_json:")
    print(json.dumps(metrics, indent=2, sort_keys=True))


async def _run_span(
    *,
    span_id: UUID,
    database: DatabaseReader,
    sink_db: DatabaseWriter | None = None,
) -> Any:
    return await execute_span(span_id, source_db=database, sink_db=sink_db)


async def _run_experiment_span(
    *,
    span_id: UUID,
    database: DatabaseReader,
    overrides: ExperimentOverrides,
    sink_db: DatabaseWriter | None = None,
) -> Any:
    result = await experiment_span(
        span_id,
        source_db=database,
        overrides=overrides,
        sink_db=sink_db,
    )
    return result.result


def _cmd_run(args: argparse.Namespace) -> int:
    imported_modules: list[str] = args.modules or []
    replay_file = Path(args.replay_file).resolve() if args.replay_file else None
    database: Database | None = None
    sink_db: FilesystemDatabase | None = None
    try:
        _import_modules(imported_modules)
        overrides = _build_overrides(args)
        if args.from_db:
            database, database_label = _resolve_database_for_span(args.db_path)
            span_id = UUID(args.from_db)
            source_label = f"database span {span_id}"
            span = asyncio.run(_load_span_from_database(database, span_id))
        else:
            if replay_file is None:
                raise ValueError("run requires a replay file or --from-db <span_id>.")
            database, database_label = _resolve_database_for_file(replay_file, args.db_path)
            source_label = replay_file.name
            span = asyncio.run(_load_span_from_file_path(replay_file, database))

        output_dir = (
            Path(args.output_dir).resolve() if args.output_dir else _default_output_dir(replay_file, args.from_db)
        )
        parent = database if isinstance(database, FilesystemDatabase) else None
        sink_db = create_debug_sink(output_dir, parent=parent)

        print(f"Replaying {span.kind} span from {source_label}")
        print(f"  span_id: {span.span_id}")
        print(f"  database: {database_label}")
        print(f"  replay output: {output_dir}\n")

        if overrides is None:
            result = asyncio.run(_run_span(span_id=span.span_id, database=database, sink_db=sink_db))
        else:
            result = asyncio.run(
                _run_experiment_span(span_id=span.span_id, database=database, overrides=overrides, sink_db=sink_db)
            )
        print(_format_result(result))
        _write_output(output_dir, result)
        print(f"\n[replay database: {output_dir}]")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        logger.debug("Replay run failed", exc_info=True)
        return 1
    finally:
        if sink_db is not None:
            try:
                asyncio.run(sink_db.flush())
                asyncio.run(sink_db.shutdown())
            except (OSError, RuntimeError, ValueError) as flush_exc:  # fmt: skip
                logger.warning("Replay sink shutdown failed: %s", flush_exc)
        if database is not None:
            try:
                asyncio.run(database.shutdown())
            except (OSError, RuntimeError, ValueError) as db_exc:  # fmt: skip
                logger.warning("Source database shutdown failed: %s", db_exc)


def _cmd_show(args: argparse.Namespace) -> int:
    replay_file = Path(args.replay_file).resolve() if args.replay_file else None
    database: Database | None = None
    try:
        if args.from_db:
            database, _database_label = _resolve_database_for_span(args.db_path)
            span = asyncio.run(_load_span_from_database(database, UUID(args.from_db)))
        else:
            if replay_file is None:
                raise ValueError("show requires a replay file or --from-db <span_id>.")
            database, _database_label = _resolve_database_for_file(replay_file, args.db_path)
            span = asyncio.run(_load_span_from_file_path(replay_file, database))
        _print_show(span)
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        if database is not None:
            asyncio.run(database.shutdown())


def _cmd_batch(args: argparse.Namespace) -> int:
    database: Database | None = None
    try:
        database, database_label = _resolve_database_for_span(args.db_path)
        _import_modules(args.modules or [])
        overrides = _build_overrides(args)
        span_ids = asyncio.run(
            find_experiment_span_ids(
                database,
                UUID(args.from_deployment),
                kind=args.kind,
                purpose=args.purpose,
                task_class=args.task_class,
            )
        )
        results = asyncio.run(
            experiment_batch(
                span_ids,
                source_db=database,
                overrides=overrides,
                concurrency=args.concurrency,
            )
        )
        print(f"Ran {len(results)} replay experiments from {database_label}")
        for result in results:
            print(
                f"{result.source_span_id} model={result.model_used or '<none>'} "
                f"duration={result.duration_seconds:.2f}s cost=${result.cost_usd:.4f}"
            )
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        logger.debug("Replay batch failed", exc_info=True)
        return 1
    finally:
        if database is not None:
            asyncio.run(database.shutdown())


def main(argv: list[str] | None = None) -> int:
    """Run the replay CLI."""
    parser = argparse.ArgumentParser(prog="ai-replay", description="Execute or inspect replayable spans")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Execute one replayable span")
    run_parser.add_argument("replay_file", nargs="?", help="Path to a span JSON file from a snapshot")
    run_parser.add_argument("--from-db", type=str, help="Load a span by span ID from the database")
    run_parser.add_argument("--db-path", type=str, help="Use a FilesystemDatabase at this path instead of ClickHouse")
    run_parser.add_argument("--model", type=str, default=None, help="Override model for replayed conversation spans")
    run_parser.add_argument(
        "--force-deployment-id",
        type=str,
        default=None,
        help="Pin replayed conversation spans to this AIPL deployment ID",
    )
    run_parser.add_argument(
        "--set", action="append", metavar="KEY=VALUE", help="Set ExperimentOverrides fields or model_options values"
    )
    run_parser.add_argument("--output-dir", type=str, default=None, help="Output directory for replay results")
    run_parser.add_argument(
        "--import", dest="modules", action="append", metavar="MODULE", help="Import a module before replay"
    )

    show_parser = subparsers.add_parser("show", help="Inspect one replayable span")
    show_parser.add_argument("replay_file", nargs="?", help="Path to a span JSON file from a snapshot")
    show_parser.add_argument("--from-db", type=str, help="Load a span by span ID from the database")
    show_parser.add_argument("--db-path", type=str, help="Use a FilesystemDatabase at this path instead of ClickHouse")

    batch_parser = subparsers.add_parser("batch", help="Run replay experiments over many spans")
    batch_parser.add_argument(
        "--from-deployment", required=True, type=str, help="Deployment/root_deployment_id to search"
    )
    batch_parser.add_argument("--db-path", type=str, help="Use a FilesystemDatabase at this path instead of ClickHouse")
    batch_parser.add_argument("--kind", type=str, default=None, help="Filter spans by kind")
    batch_parser.add_argument("--purpose", type=str, default=None, help="Filter conversation spans by purpose")
    batch_parser.add_argument("--task-class", type=str, default=None, help="Filter task spans by module:Class path")
    batch_parser.add_argument("--model", type=str, default=None, help="Override model for replayed conversation spans")
    batch_parser.add_argument(
        "--force-deployment-id",
        type=str,
        default=None,
        help="Pin replayed conversation spans to this AIPL deployment ID",
    )
    batch_parser.add_argument(
        "--set", action="append", metavar="KEY=VALUE", help="Set ExperimentOverrides fields or model_options values"
    )
    batch_parser.add_argument("--concurrency", type=int, default=5, help="Maximum concurrent experiments")
    batch_parser.add_argument(
        "--import", dest="modules", action="append", metavar="MODULE", help="Import a module before replay"
    )

    args = parser.parse_args(argv)

    if args.command == "run":
        if args.replay_file is None and args.from_db is None:
            parser.error("run requires either a replay_file or --from-db <span_id>")
        if args.replay_file is not None and args.from_db is not None:
            parser.error("run accepts a replay_file or --from-db <span_id>, not both")
        return _cmd_run(args)
    if args.command == "show":
        if args.replay_file is None and args.from_db is None:
            parser.error("show requires either a replay_file or --from-db <span_id>")
        if args.replay_file is not None and args.from_db is not None:
            parser.error("show accepts a replay_file or --from-db <span_id>, not both")
        return _cmd_show(args)
    if args.command == "batch":
        return _cmd_batch(args)

    parser.print_help()
    return 1
