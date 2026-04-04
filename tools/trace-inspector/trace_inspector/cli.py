"""CLI for trace-inspector."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from uuid import UUID

from trace_inspector._api import inspect_run
from trace_inspector._compare import compare_runs
from trace_inspector._types import ComparisonSelection, RenderConfig, Selection, SourceSpec

__all__ = [
    "main",
]


def main(argv: list[str] | None = None) -> int:
    """Run the trace-inspector CLI."""
    parser = argparse.ArgumentParser(prog="ai-trace-inspect", description="Render ai-pipeline-core traces as markdown bundles")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Inspect a full run")
    _add_source_arguments(run_parser)
    run_parser.add_argument("--out", required=True, type=Path, help="Output directory")
    run_parser.add_argument("--failed", action="store_true", help="Render failed tasks only")
    run_parser.add_argument("--flow-name", type=str, default=None, help="Restrict output to one flow name")

    flow_parser = subparsers.add_parser("flow", help="Inspect one flow")
    _add_source_arguments(flow_parser)
    flow_parser.add_argument("--out", required=True, type=Path, help="Output directory")
    flow_group = flow_parser.add_mutually_exclusive_group(required=True)
    flow_group.add_argument("--flow-name", type=str, default=None, help="Target flow name")
    flow_group.add_argument("--flow-span-id", type=UUID, default=None, help="Target flow span id")

    task_parser = subparsers.add_parser("task", help="Inspect one task with provenance neighbors")
    _add_source_arguments(task_parser)
    task_parser.add_argument("--out", required=True, type=Path, help="Output directory")
    task_parser.add_argument("--task-span-id", required=True, type=UUID, help="Target task span id")

    compare_parser = subparsers.add_parser("compare", help="Compare one task across two traces")
    _add_named_source_arguments(compare_parser, prefix="left")
    _add_named_source_arguments(compare_parser, prefix="right")
    compare_parser.add_argument("--out", required=True, type=Path, help="Output directory")
    compare_parser.add_argument("--left-task-span-id", required=True, type=UUID, help="Task span id from the left/original trace")
    compare_parser.add_argument("--right-task-span-id", type=UUID, default=None, help="Task span id from the right/replay trace")

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 1

    try:
        if args.command == "run":
            selection = Selection(
                mode="run",
                source=_source_from_args(args),
                output_dir=args.out.resolve(),
                render_config=RenderConfig(),
                failed_only=args.failed,
                flow_name=args.flow_name,
            )
            asyncio.run(inspect_run(selection))
            return 0

        if args.command == "flow":
            selection = Selection(
                mode="flow",
                source=_source_from_args(args),
                output_dir=args.out.resolve(),
                render_config=RenderConfig(),
                flow_name=args.flow_name,
                flow_span_id=args.flow_span_id,
            )
            asyncio.run(inspect_run(selection))
            return 0

        if args.command == "task":
            selection = Selection(
                mode="task",
                source=_source_from_args(args),
                output_dir=args.out.resolve(),
                render_config=RenderConfig(),
                task_span_id=args.task_span_id,
            )
            asyncio.run(inspect_run(selection))
            return 0

        if args.command == "compare":
            selection = ComparisonSelection(
                left_source=_named_source_from_args(args, "left"),
                right_source=_named_source_from_args(args, "right"),
                output_dir=args.out.resolve(),
                left_task_span_id=args.left_task_span_id,
                right_task_span_id=args.right_task_span_id,
                render_config=RenderConfig(),
            )
            asyncio.run(compare_runs(selection))
            return 0
    except Exception as exc:
        raise SystemExit(f"Trace inspector failed: {exc}") from exc

    return 1


def _add_source_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--db-path", type=Path, default=None, help="FilesystemDatabase path")
    group.add_argument("--run-id", type=str, default=None, help="ClickHouse run id")
    group.add_argument("--deployment-id", type=UUID, default=None, help="ClickHouse deployment id")
    parser.add_argument("--download-db-to", type=Path, default=None, help="Download a ClickHouse deployment into a FilesystemDatabase snapshot first")


def _add_named_source_arguments(parser: argparse.ArgumentParser, prefix: str) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(f"--{prefix}-db-path", type=Path, default=None, help=f"{prefix.title()} FilesystemDatabase path")
    group.add_argument(f"--{prefix}-run-id", type=str, default=None, help=f"{prefix.title()} ClickHouse run id")
    group.add_argument(f"--{prefix}-deployment-id", type=UUID, default=None, help=f"{prefix.title()} ClickHouse deployment id")
    parser.add_argument(f"--download-{prefix}-db-to", type=Path, default=None, help=f"Download the {prefix} ClickHouse deployment first")


def _source_from_args(args: argparse.Namespace) -> SourceSpec:
    return SourceSpec(
        db_path=args.db_path.resolve() if args.db_path is not None else None,
        run_id=args.run_id,
        deployment_id=args.deployment_id,
        download_db_to=args.download_db_to.resolve() if args.download_db_to is not None else None,
    )


def _named_source_from_args(args: argparse.Namespace, prefix: str) -> SourceSpec:
    db_path = getattr(args, f"{prefix}_db_path")
    download_db_to = getattr(args, f"download_{prefix}_db_to")
    return SourceSpec(
        db_path=None if db_path is None else db_path.resolve(),
        run_id=getattr(args, f"{prefix}_run_id"),
        deployment_id=getattr(args, f"{prefix}_deployment_id"),
        download_db_to=None if download_db_to is None else download_db_to.resolve(),
    )
