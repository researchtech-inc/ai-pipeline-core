"""Internal validation for portable filesystem snapshot bundles."""

import json
from pathlib import Path
from typing import Any

from ai_pipeline_core.database.filesystem._backend import (
    BLOBS_DIRNAME,
    DOCUMENTS_DIRNAME,
    LOGS_FILENAME,
    RUNS_DIRNAME,
    FilesystemDatabase,
)

__all__ = ["validate_bundle"]


def validate_bundle(bundle_path: Path) -> dict[str, Any]:
    """Validate a staged filesystem snapshot before it is published."""
    _require_path(bundle_path, is_dir=True, label="bundle root")
    _require_path(bundle_path / RUNS_DIRNAME, is_dir=True, label="runs directory")
    _require_path(bundle_path / DOCUMENTS_DIRNAME, is_dir=True, label="documents directory")
    _require_path(bundle_path / BLOBS_DIRNAME, is_dir=True, label="blobs directory")
    _require_path(bundle_path / LOGS_FILENAME, is_dir=False, label="logs file")
    schema_meta_path = bundle_path / "schema_meta.json"
    if not schema_meta_path.is_file():
        raise FileNotFoundError(
            f"Snapshot validation failed because the required schema_meta.json is missing at {schema_meta_path}. "
            "Include schema_meta.json in the snapshot bundle for provenance tracking."
        )

    snapshot = FilesystemDatabase(bundle_path, read_only=True)
    referenced_document_shas: set[str] = set()
    referenced_blob_shas: set[str] = set()

    for span in snapshot.loaded_spans.values():
        referenced_document_shas.update(span.input_document_shas)
        referenced_document_shas.update(span.output_document_shas)
        referenced_blob_shas.update(span.input_blob_shas)
        referenced_blob_shas.update(span.output_blob_shas)

    for document in snapshot.loaded_documents.values():
        referenced_blob_shas.add(document.content_sha256)
        referenced_blob_shas.update(document.attachment_content_sha256s)

    missing_documents = sorted(referenced_document_shas - set(snapshot.loaded_documents))
    if missing_documents:
        raise ValueError(
            "Snapshot validation failed because some referenced documents are missing from documents/: "
            + ", ".join(missing_documents)
        )

    stored_blob_shas = {path.name for path in (bundle_path / BLOBS_DIRNAME).iterdir() if path.is_file()}
    missing_content_blobs = sorted(referenced_blob_shas - stored_blob_shas)
    if missing_content_blobs:
        raise ValueError(
            "Snapshot validation failed because some referenced blobs are missing from blobs/: "
            + ", ".join(missing_content_blobs)
        )

    return {
        "valid": True,
        "span_count": len(snapshot.loaded_spans),
        "document_count": len(snapshot.loaded_documents),
        "blob_count": len(stored_blob_shas),
        "log_count": _count_log_lines(bundle_path / LOGS_FILENAME),
    }


def _require_path(path: Path, *, is_dir: bool, label: str) -> None:
    if not path.exists():
        raise FileNotFoundError(
            f"Snapshot validation failed because the required {label} is missing at {path}. "
            "Recreate the bundle so the filesystem snapshot is complete before publishing it."
        )
    if is_dir and not path.is_dir():
        raise NotADirectoryError(
            f"Snapshot validation expected {label} at {path} to be a directory. "
            "Recreate the bundle so the snapshot layout matches runs/, documents/, and blobs/ directories."
        )
    if not is_dir and not path.is_file():
        raise ValueError(
            f"Snapshot validation expected {label} at {path} to be a file. "
            "Recreate the bundle so logs.jsonl is written before publication."
        )


def _count_log_lines(logs_path: Path) -> int:
    count = 0
    with logs_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Snapshot validation failed because logs.jsonl contains invalid JSON on line {line_number}. "
                    "Recreate the bundle or repair the corrupted log entry."
                ) from exc
            if not isinstance(parsed, dict):
                raise TypeError(
                    f"Snapshot validation expected each logs.jsonl line to be a JSON object, "
                    f"got {type(parsed).__name__} on line {line_number}. "
                    "Rewrite the snapshot log file as JSON Lines with one object per line."
                )
            count += 1
    return count
