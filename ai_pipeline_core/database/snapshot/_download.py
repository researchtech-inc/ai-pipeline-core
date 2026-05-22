"""Export deployment data as a portable FilesystemDatabase snapshot."""

import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID

from ai_pipeline_core.database._protocol import DatabaseReader
from ai_pipeline_core.database._types import DocumentRecord, SpanRecord
from ai_pipeline_core.database.filesystem._backend import FilesystemDatabase
from ai_pipeline_core.database.filesystem._validation import validate_bundle

__all__ = ["download_deployment"]

VALIDATION_FILENAME = "validation.json"


def _collect_attachment_blob_shas(documents: list[DocumentRecord]) -> set[str]:
    blob_shas: set[str] = set()
    for document in documents:
        blob_shas.add(document.content_sha256)
        blob_shas.update(document.attachment_content_sha256s)
    return blob_shas


def _collect_span_blob_shas(tree: list[SpanRecord]) -> set[str]:
    blob_shas: set[str] = set()
    for span in tree:
        blob_shas.update(span.input_blob_shas)
        blob_shas.update(span.output_blob_shas)
    return blob_shas


def _raise_if_missing_records(
    *,
    record_kind: str,
    expected_shas: set[str],
    actual_shas: set[str],
    deployment_id: UUID,
) -> None:
    missing_shas = sorted(expected_shas - actual_shas)
    if not missing_shas:
        return
    missing_list = ", ".join(missing_shas)
    singular = record_kind[:-1] if record_kind.endswith("s") else record_kind
    msg = (
        f"download_deployment({deployment_id}) could not produce a complete snapshot because "
        f"{record_kind} are missing from the source database: {missing_list}. "
        f"Persist every referenced {singular} before downloading the deployment tree."
    )
    raise ValueError(msg)


def _remove_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
        return
    path.unlink()


def _create_staging_path(output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f".{output_path.name}-staging-", dir=output_path.parent))


def _replace_output_path(staged_path: Path, output_path: Path) -> None:
    backup_path = output_path.with_name(f".{output_path.name}-backup")
    _remove_path(backup_path)
    if not output_path.exists():
        staged_path.replace(output_path)
        return
    output_path.replace(backup_path)
    try:
        staged_path.replace(output_path)
    except Exception:
        backup_path.replace(output_path)
        raise
    _remove_path(backup_path)


async def _get_tree_logs(
    source: DatabaseReader,
    deployment_ids: list[UUID],
) -> list[Any]:
    return await source.get_deployment_logs_batch(deployment_ids)


async def download_deployment(
    source: DatabaseReader,
    deployment_id: UUID,
    output_path: Path,
) -> None:
    """Export a deployment tree as a FilesystemDatabase snapshot."""
    tree = await source.get_deployment_tree(deployment_id)
    staged_path = await asyncio.to_thread(_create_staging_path, output_path)
    target = await asyncio.to_thread(FilesystemDatabase, staged_path)
    committed = False
    try:
        for span in tree:
            await target.insert_span(span)

        document_shas = await source.get_all_document_shas_for_tree(deployment_id)
        documents = await source.get_documents_batch(sorted(document_shas))
        _raise_if_missing_records(
            record_kind="documents",
            expected_shas=document_shas,
            actual_shas=set(documents),
            deployment_id=deployment_id,
        )
        if documents:
            await target.save_document_batch(list(documents.values()))

        blob_shas = _collect_attachment_blob_shas(list(documents.values()))
        blob_shas.update(_collect_span_blob_shas(tree))
        blobs = await source.get_blobs_batch(sorted(blob_shas))
        _raise_if_missing_records(
            record_kind="blobs",
            expected_shas=blob_shas,
            actual_shas=set(blobs),
            deployment_id=deployment_id,
        )
        if blobs:
            await target.save_blob_batch(list(blobs.values()))

        deployment_ids = sorted({span.deployment_id for span in tree}, key=str)
        logs = await _get_tree_logs(source, deployment_ids)
        if logs:
            await target.save_logs_batch(logs)
        else:
            await asyncio.to_thread((staged_path / "logs.jsonl").write_text, "", encoding="utf-8")

        schema_meta = {"schema_version": 3, "source": "download_deployment"}
        await asyncio.to_thread(
            (staged_path / "schema_meta.json").write_text,
            json.dumps(schema_meta, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        await target.shutdown()
        validation = await asyncio.to_thread(validate_bundle, staged_path)
        await asyncio.to_thread(
            (staged_path / VALIDATION_FILENAME).write_text,
            json.dumps(validation, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        await asyncio.to_thread(_replace_output_path, staged_path, output_path)
        committed = True
    finally:
        if not committed:
            await target.shutdown()
            await asyncio.to_thread(_remove_path, staged_path)
