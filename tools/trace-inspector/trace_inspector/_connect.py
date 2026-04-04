"""Database connection helpers for trace-inspector."""

from uuid import UUID

from ai_pipeline_core.database._protocol import DatabaseReader
from ai_pipeline_core.database.clickhouse import ClickHouseDatabase
from ai_pipeline_core.database.filesystem import FilesystemDatabase
from ai_pipeline_core.database.snapshot import download_deployment
from ai_pipeline_core.settings import settings
from trace_inspector._types import ReaderConnection, SourceSpec

__all__ = [
    "open_reader_from_source",
]


async def open_reader_from_source(source: SourceSpec) -> ReaderConnection:
    """Open the configured database source and resolve the target deployment id."""
    if source.db_path is not None:
        reader = FilesystemDatabase(source.db_path.resolve(), read_only=True)
        deployment_id = await _resolve_filesystem_deployment_id(reader, source)
        return ReaderConnection(reader=reader, deployment_id=deployment_id, reader_label=str(source.db_path.resolve()))

    reader = ClickHouseDatabase(settings=settings)
    deployment_id = await _resolve_clickhouse_deployment_id(reader, source)
    if source.download_db_to is None:
        return ReaderConnection(reader=reader, deployment_id=deployment_id, reader_label=f"clickhouse:{deployment_id}")

    download_dir = source.download_db_to.resolve()
    await download_deployment(reader, deployment_id, download_dir)
    await reader.shutdown()
    filesystem_reader = FilesystemDatabase(download_dir, read_only=True)
    return ReaderConnection(reader=filesystem_reader, deployment_id=deployment_id, reader_label=str(download_dir))


async def _resolve_filesystem_deployment_id(reader: FilesystemDatabase, source: SourceSpec) -> UUID:
    if source.deployment_id is not None:
        return source.deployment_id
    if source.run_id is not None:
        deployment = await reader.get_deployment_by_run_id(source.run_id)
        if deployment is None:
            msg = (
                f"Run id {source.run_id!r} was not found in the FilesystemDatabase snapshot. "
                "Pass a valid run_id from ai-trace list, or omit run_id when the snapshot contains exactly one deployment."
            )
            raise ValueError(msg)
        return deployment.root_deployment_id

    deployments = await reader.list_deployments(limit=2)
    if len(deployments) != 1:
        msg = (
            f"FilesystemDatabase {reader.base_path} contains {len(deployments)} deployment roots. "
            "Pass --run-id or --deployment-id so trace-inspector knows which deployment tree to load."
        )
        raise ValueError(msg)
    return deployments[0].root_deployment_id


async def _resolve_clickhouse_deployment_id(reader: DatabaseReader, source: SourceSpec) -> UUID:
    if source.deployment_id is not None:
        return source.deployment_id
    if source.run_id is None:
        msg = "ClickHouse trace inspection requires either run_id or deployment_id. Pass --run-id or --deployment-id."
        raise ValueError(msg)
    deployment = await reader.get_deployment_by_run_id(source.run_id)
    if deployment is None:
        msg = f"Run id {source.run_id!r} was not found in ClickHouse. Pass a valid run_id or deployment_id."
        raise ValueError(msg)
    return deployment.root_deployment_id
