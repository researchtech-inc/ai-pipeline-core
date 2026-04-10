"""CLI tool for span-tree inspection."""

import argparse
import asyncio
import sys
from pathlib import Path
from uuid import UUID

from prefect import get_client  # noqa: TID251 - NEXT-STEPS v2 requires module-level Prefect import here.

from ai_pipeline_core.database._factory import Database
from ai_pipeline_core.database._protocol import DatabaseReader
from ai_pipeline_core.database._types import SpanKind, SpanRecord
from ai_pipeline_core.database.clickhouse._backend import ClickHouseDatabase
from ai_pipeline_core.database.filesystem._backend import FilesystemDatabase
from ai_pipeline_core.database.snapshot._download import download_deployment
from ai_pipeline_core.database.snapshot._spans import build_span_tree_view
from ai_pipeline_core.logger._logging_config import setup_logging
from ai_pipeline_core.logger._types import LogRecord
from ai_pipeline_core.observability._recovery import recover_orphaned_spans
from ai_pipeline_core.observability._tree_render import format_span_overview_lines, format_span_tree_lines
from ai_pipeline_core.settings import settings

__all__ = [
    "_parse_execution_id",
    "_resolve_connection",
    "_resolve_identifier",
    "main",
]

_TraceDatabase = Database | FilesystemDatabase | ClickHouseDatabase
_RECOVER_SCOPE_WARNING = (
    "This tool only acts on explicit Prefect-terminal signals (FAILED, CRASHED, CANCELLED, COMPLETED). "
    "Runs in RUNNING state or returning ObjectNotFound will be left untouched. "
    "Use --i-accept-nondeterministic-reconciliation to force wall-clock reconciliation for operator-investigated cases."
)


def _parse_execution_id(value: str) -> UUID:
    """Parse a CLI execution identifier."""
    try:
        return UUID(value)
    except ValueError as exc:
        raise SystemExit(f"Invalid execution id {value!r}. Expected a UUID.") from exc


def _resolve_connection(args: argparse.Namespace) -> _TraceDatabase:
    """Resolve CLI connection parameters."""
    if getattr(args, "db_path", None):
        base_path = Path(args.db_path).resolve()
        read_only = getattr(args, "command", None) != "recover"
        return FilesystemDatabase(base_path, read_only=read_only)

    if not settings.clickhouse_host:
        raise SystemExit("ClickHouse is not configured. Set CLICKHOUSE_HOST or use --db-path with a FilesystemDatabase snapshot.")
    return ClickHouseDatabase(settings=settings)


async def _resolve_identifier_async(identifier: str, client: DatabaseReader) -> tuple[UUID, str]:
    """Resolve a deployment or span identifier to a concrete deployment."""
    try:
        execution_id = _parse_execution_id(identifier)
    except SystemExit:
        execution_id = None

    if execution_id is not None:
        span = await client.get_span(execution_id)
        if span is not None:
            return span.root_deployment_id, span.run_id

    deployment = await client.get_deployment_by_run_id(identifier)
    if deployment is not None:
        return deployment.root_deployment_id, deployment.run_id

    raise SystemExit(f"Could not resolve {identifier!r} to a deployment. Pass a deployment/span UUID or a known run_id from ai-trace list.")


def _resolve_identifier(identifier: str, client: DatabaseReader) -> tuple[UUID, str]:
    """Resolve a CLI identifier to a concrete deployment."""
    return asyncio.run(_resolve_identifier_async(identifier, client))


def _format_duration(node: SpanRecord) -> str:
    """Format a deployment duration for list output."""
    if node.ended_at is None:
        return "running"
    return str(node.ended_at - node.started_at)


def _log_sort_key(log: LogRecord) -> tuple[object, int, str]:
    return log.timestamp, log.sequence_no, str(log.span_id)


def _print_logs(logs: list[LogRecord]) -> None:
    """Render execution logs in chronological order."""
    if not logs:
        print("\nLogs: none")
        return

    print("\nLogs")
    for log in sorted(logs, key=_log_sort_key):
        timestamp = log.timestamp.isoformat()
        print(f"{timestamp} {log.level} {log.category} {log.logger_name}: {log.message}")
        fields = log.fields_json
        if fields and fields != "{}":
            print(f"  fields: {fields}")
        if log.exception_text:
            for line in log.exception_text.splitlines():
                print(f"  {line}")


async def _get_tree_logs(database: DatabaseReader, deployment_ids: list[UUID]) -> list[LogRecord]:
    return await database.get_deployment_logs_batch(deployment_ids)


def _render_deployment_v2(tree: list[SpanRecord], root_deployment_id: UUID) -> str:
    view = build_span_tree_view(tree, root_deployment_id)
    if view is None:
        return "No execution data found."

    lines = format_span_overview_lines(view)
    lines.extend(["", "Tree", ""])
    lines.extend(format_span_tree_lines(view))
    return "\n".join(lines)


async def _list_deployments_async(database: _TraceDatabase, limit: int, status: str | None) -> int:
    try:
        deployments = await database.list_deployments(limit=limit, status=status)
    finally:
        await database.shutdown()

    if not deployments:
        print("No deployments found.")
        return 0

    for node in deployments:
        status_text = getattr(node.status, "value", str(node.status))
        print(
            f"{node.deployment_id}  {status_text:9}  {node.started_at.isoformat()}  "
            f"{node.deployment_name}  run_id={node.run_id}  duration={_format_duration(node)}"
        )
    return 0


async def _show_deployment_async(database: _TraceDatabase, identifier: str) -> int:
    try:
        deployment_id, _run_id = await _resolve_identifier_async(identifier, database)
        tree = await database.get_deployment_tree(deployment_id)
        summary = _render_deployment_v2(tree, deployment_id)
        logs = await _get_tree_logs(database, sorted({span.deployment_id for span in tree}, key=str))
    finally:
        await database.shutdown()

    print(summary)
    _print_logs(logs)
    return 0


async def _download_deployment_async(
    database: _TraceDatabase,
    identifier: str,
    output_dir: Path,
) -> int:
    try:
        deployment_id, _run_id = await _resolve_identifier_async(identifier, database)
        await download_deployment(database, deployment_id, output_dir)
    finally:
        await database.shutdown()
    print(f"Downloaded deployment to {output_dir}")
    return 0


async def _show_llm_calls_async(database: _TraceDatabase, identifier: str) -> int:
    """Show all LLM calls in a deployment."""
    import json

    try:
        deployment_id, _run_id = await _resolve_identifier_async(identifier, database)
        tree = await database.get_deployment_tree(deployment_id)
    finally:
        await database.shutdown()

    llm_spans = [s for s in tree if s.kind == SpanKind.LLM_ROUND]
    if not llm_spans:
        print("No LLM calls found.")
        return 0

    for i, span in enumerate(llm_spans, 1):
        meta = json.loads(span.meta_json) if span.meta_json else {}
        metrics = json.loads(span.metrics_json) if span.metrics_json else {}
        model = meta.get("model", "unknown")
        purpose = meta.get("purpose", "")
        tokens_in = metrics.get("tokens_input", 0)
        tokens_out = metrics.get("tokens_output", 0)
        cost = span.cost_usd
        duration = ""
        if span.started_at and span.ended_at:
            duration = f" ({(span.ended_at - span.started_at).total_seconds():.1f}s)"

        print(f"[{i}] {model}  tokens={tokens_in}→{tokens_out}  cost=${cost:.4f}{duration}")
        if purpose:
            print(f"    purpose: {purpose}")
        print()
    return 0


async def _show_docs_async(database: _TraceDatabase, identifier: str) -> int:
    """List all documents in a deployment."""
    try:
        deployment_id, _run_id = await _resolve_identifier_async(identifier, database)
        doc_shas = await database.get_all_document_shas_for_tree(deployment_id)
        documents = await database.get_documents_batch(sorted(doc_shas))
    finally:
        await database.shutdown()

    if not documents:
        print("No documents found.")
        return 0

    print(f"{'Type':<35} {'Name':<30} {'Size':>8}  SHA256")
    print("-" * 100)
    for sha, doc in sorted(documents.items(), key=lambda kv: kv[1].document_type):
        print(f"{doc.document_type:<35} {doc.name:<30} {doc.size_bytes:>8}  {sha}")
    return 0


async def _show_doc_async(database: _TraceDatabase, sha256: str) -> int:
    """Show a single document by SHA256."""
    try:
        hydrated = await database.get_document_with_content(sha256)
    finally:
        await database.shutdown()

    if hydrated is None:
        print(f"Document {sha256!r} not found.", file=sys.stderr)
        return 1

    print(f"Type: {hydrated.record.document_type}")
    print(f"Name: {hydrated.record.name}")
    print(f"Size: {hydrated.record.size_bytes} bytes")
    print(f"MIME: {hydrated.record.mime_type}")
    if hydrated.record.derived_from:
        print(f"Derived from: {', '.join(hydrated.record.derived_from)}")
    if hydrated.record.triggered_by:
        print(f"Triggered by: {', '.join(hydrated.record.triggered_by)}")
    print()
    if hydrated.content is not None:
        try:
            text = hydrated.content.decode("utf-8")
        except UnicodeDecodeError:
            print(f"[Binary content, {hydrated.record.size_bytes} bytes]")
        else:
            print(text)
    return 0


async def _recover_orphans_async(
    database: _TraceDatabase,
    *,
    accept_nondeterministic_reconciliation: bool,
) -> int:
    try:
        if accept_nondeterministic_reconciliation:
            recovered = await recover_orphaned_spans(
                database,
                prefect_client=None,
                require_prefect_client=False,
            )
        else:
            async with get_client() as prefect_client:
                recovered = await recover_orphaned_spans(
                    database,
                    prefect_client=prefect_client,
                    require_prefect_client=True,
                )
    finally:
        await database.shutdown()
    print(f"Recovered {len(recovered)} orphaned run(s).")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the ai-trace CLI."""
    setup_logging()
    db_parent = argparse.ArgumentParser(add_help=False)
    db_parent.add_argument("--db-path", type=str, default=None, help="Use a FilesystemDatabase snapshot instead of ClickHouse")

    parser = argparse.ArgumentParser(prog="ai-trace", description="Inspect deployment execution trees")
    subparsers = parser.add_subparsers(dest="command")

    list_parser = subparsers.add_parser("list", parents=[db_parent], help="List recent deployments")
    list_parser.add_argument("--limit", type=int, default=20, help="Maximum number of deployments to show")
    list_parser.add_argument("--status", type=str, default=None, help="Filter deployments by status")

    show_parser = subparsers.add_parser("show", parents=[db_parent], help="Show deployment summary and logs")
    show_parser.add_argument("identifier", help="Deployment/span UUID or deployment run_id")

    download_parser = subparsers.add_parser("download", parents=[db_parent], help="Download a deployment as a FilesystemDatabase snapshot")
    download_parser.add_argument("identifier", help="Deployment/span UUID or deployment run_id")
    download_parser.add_argument("-o", "--output-dir", type=str, required=True, help="Output directory for the snapshot")

    llm_parser = subparsers.add_parser("llm", parents=[db_parent], help="Show all LLM calls for a deployment")
    llm_parser.add_argument("identifier", help="Deployment/span UUID or deployment run_id")

    docs_parser = subparsers.add_parser("docs", parents=[db_parent], help="List all documents in a deployment")
    docs_parser.add_argument("identifier", help="Deployment/span UUID or deployment run_id")

    doc_parser = subparsers.add_parser("doc", parents=[db_parent], help="Show a single document by SHA256")
    doc_parser.add_argument("sha256", help="Document SHA256 identifier")

    recover_parser = subparsers.add_parser("recover", parents=[db_parent], help="Mark orphaned running deployments as failed")
    recover_parser.add_argument(
        "--i-accept-nondeterministic-reconciliation",
        action="store_true",
        default=False,
        help=("Explicitly bypass Prefect-state reconciliation and force nondeterministic wall-clock reconciliation after operator investigation."),
    )

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 1

    try:
        database = _resolve_connection(args)
        if args.command == "list":
            return asyncio.run(_list_deployments_async(database, args.limit, args.status))
        if args.command == "show":
            return asyncio.run(_show_deployment_async(database, args.identifier))
        if args.command == "download":
            return asyncio.run(_download_deployment_async(database, args.identifier, Path(args.output_dir).resolve()))
        if args.command == "llm":
            return asyncio.run(_show_llm_calls_async(database, args.identifier))
        if args.command == "docs":
            return asyncio.run(_show_docs_async(database, args.identifier))
        if args.command == "doc":
            return asyncio.run(_show_doc_async(database, args.sha256))
        if args.command == "recover":
            print(_RECOVER_SCOPE_WARNING)
            return asyncio.run(
                _recover_orphans_async(
                    database,
                    accept_nondeterministic_reconciliation=args.i_accept_nondeterministic_reconciliation,
                )
            )
    except SystemExit:
        raise
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 1
