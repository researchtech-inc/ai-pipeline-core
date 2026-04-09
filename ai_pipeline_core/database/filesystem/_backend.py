"""Filesystem backend for the redesigned span/document/blob/log schema."""

import asyncio
import contextlib
import json
import logging
import os
import shutil
import tempfile
import threading
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar
from uuid import UUID

from ai_pipeline_core.database._documents import _attachment_contents_for_record
from ai_pipeline_core.database._json_helpers import parse_json_object
from ai_pipeline_core.database._serialization import (
    LOG_COLUMNS,
    SPAN_COLUMNS,
    row_to_log,
    row_to_span,
)
from ai_pipeline_core.database._sorting import (
    child_span_sort_key,
    deployment_sort_key,
    log_sort_key,
    span_sort_key,
    tree_child_sort_key,
)
from ai_pipeline_core.database._types import (
    CostTotals,
    DocumentRecord,
    HydratedDocument,
    LogRecord,
    SpanKind,
    SpanRecord,
    SpanStatus,
    _BlobRecord,
    aggregate_cost_totals,
)
from ai_pipeline_core.database.filesystem._paths import run_directory_name, span_filename

__all__ = ["FilesystemDatabase"]

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

SPANS_DIRNAME = "spans"
RUNS_DIRNAME = "runs"
DOCUMENTS_DIRNAME = "documents"
BLOBS_DIRNAME = "blobs"
LOGS_FILENAME = "logs.jsonl"


def _json_default(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    msg = (
        f"FilesystemDatabase can only JSON-serialize UUID and datetime helper types automatically. "
        f"Convert {type(value).__name__} values before writing them to the snapshot."
    )
    raise TypeError(msg)


def _atomic_write(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = data.encode("utf-8") if isinstance(data, str) else data
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def _serialize_record(record: SpanRecord | DocumentRecord | LogRecord) -> dict[str, Any]:
    values = asdict(record)
    keys: tuple[str, ...]
    if isinstance(record, SpanRecord):
        keys = SPAN_COLUMNS
    elif isinstance(record, DocumentRecord):
        return values
    else:
        keys = LOG_COLUMNS
    return {key: values[key] for key in keys}


def _read_json_dict(path: Path, *, context: str) -> dict[str, Any]:
    try:
        raw_value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        msg = f"{context} at {path} is not valid JSON. Recreate the snapshot or fix the corrupted file."
        raise ValueError(msg) from exc
    if not isinstance(raw_value, dict):
        msg = f"{context} at {path} must contain a JSON object. Rewrite the file as an object like {{}}."
        raise TypeError(msg)
    return raw_value


def _deserialize_span(data: dict[str, Any], *, path: Path) -> SpanRecord:
    try:
        row = tuple(data[column] for column in SPAN_COLUMNS)
    except KeyError as exc:
        missing_field = exc.args[0]
        msg = (
            f"Span snapshot file {path} is missing required field {missing_field!r}. "
            "Recreate the download bundle so every span JSON file contains the full SpanRecord payload."
        )
        raise ValueError(msg) from exc
    return row_to_span(row)


def _deserialize_document(data: dict[str, Any], *, path: Path) -> DocumentRecord:
    try:
        return DocumentRecord(
            document_sha256=data["document_sha256"],
            content_sha256=data["content_sha256"],
            document_type=data["document_type"],
            name=data["name"],
            description=data["description"],
            mime_type=data["mime_type"],
            size_bytes=data["size_bytes"],
            summary=data["summary"],
            derived_from=tuple(data["derived_from"]),
            triggered_by=tuple(data["triggered_by"]),
            attachment_names=tuple(data["attachment_names"]),
            attachment_descriptions=tuple(data["attachment_descriptions"]),
            attachment_content_sha256s=tuple(data["attachment_content_sha256s"]),
            attachment_mime_types=tuple(data["attachment_mime_types"]),
            attachment_size_bytes=tuple(data["attachment_size_bytes"]),
            publicly_visible=data.get("publicly_visible", False),
        )
    except KeyError as exc:
        missing_field = exc.args[0]
        msg = (
            f"Document snapshot file {path} is missing required field {missing_field!r}. "
            "Recreate the download bundle so every document JSON file contains the full DocumentRecord payload."
        )
        raise ValueError(msg) from exc


def _deserialize_log(data: dict[str, Any], *, path: Path) -> LogRecord:
    try:
        row = tuple(data[column] for column in LOG_COLUMNS)
    except KeyError as exc:
        missing_field = exc.args[0]
        msg = (
            f"Log snapshot entry from {path} is missing required field {missing_field!r}. "
            "Rewrite the snapshot log file so each line contains the full LogRecord payload."
        )
        raise ValueError(msg) from exc
    return row_to_log(row)


class FilesystemDatabase:
    """Filesystem snapshot backend for the redesigned span protocols."""

    supports_remote = False

    def __init__(self, base_path: Path, *, read_only: bool = False) -> None:
        self._base_path = base_path
        self._read_only = read_only
        if self._read_only:
            if not self._base_path.exists():
                raise FileNotFoundError(
                    f"FilesystemDatabase read-only path {self._base_path} does not exist. "
                    "Pass an existing snapshot root when opening a portable bundle read-only."
                )
            if not self._base_path.is_dir():
                raise NotADirectoryError(
                    f"FilesystemDatabase read-only path {self._base_path} is not a directory. "
                    "Pass the snapshot root directory that contains runs/, documents/, blobs/, and logs.jsonl."
                )
        else:
            self._base_path.mkdir(parents=True, exist_ok=True)
            self._spans_dir.mkdir(parents=True, exist_ok=True)
            self._runs_dir.mkdir(parents=True, exist_ok=True)
            self._documents_dir.mkdir(parents=True, exist_ok=True)
            self._blobs_dir.mkdir(parents=True, exist_ok=True)

        self._spans: dict[UUID, SpanRecord] = {}
        self._span_paths: dict[UUID, Path] = {}
        self._documents: dict[str, DocumentRecord] = {}
        self._document_paths: dict[str, Path] = {}
        self._logs: list[LogRecord] = []
        self._write_lock = threading.Lock()
        self._load_from_disk()

    @property
    def base_path(self) -> Path:
        return self._base_path

    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def loaded_spans(self) -> dict[UUID, SpanRecord]:
        """All loaded span records keyed by span_id (read-only snapshot data)."""
        return self._spans

    @property
    def loaded_documents(self) -> dict[str, DocumentRecord]:
        """All loaded document records keyed by document_sha256 (read-only snapshot data)."""
        return self._documents

    @property
    def _spans_dir(self) -> Path:
        return self._base_path / SPANS_DIRNAME

    @property
    def _documents_dir(self) -> Path:
        return self._base_path / DOCUMENTS_DIRNAME

    @property
    def _runs_dir(self) -> Path:
        return self._base_path / RUNS_DIRNAME

    @property
    def _blobs_dir(self) -> Path:
        return self._base_path / BLOBS_DIRNAME

    @property
    def _logs_path(self) -> Path:
        return self._base_path / LOGS_FILENAME

    async def _run(self, fn: Callable[..., _T], *args: Any) -> _T:
        return await asyncio.to_thread(fn, *args)

    async def _run_write(self, fn: Callable[..., _T], *args: Any) -> _T:
        def _locked() -> _T:
            with self._write_lock:
                return fn(*args)

        return await asyncio.to_thread(_locked)

    def _store_span_from_disk(self, span: SpanRecord, *, path: Path) -> None:
        existing = self._spans.get(span.span_id)
        if existing is not None and existing.version >= span.version:
            return
        self._spans[span.span_id] = span
        self._span_paths[span.span_id] = path

    def _load_tree_span_file(self, span_path: Path) -> None:
        payload = _read_json_dict(span_path, context="Span snapshot")
        self._store_span_from_disk(_deserialize_span(payload, path=span_path), path=span_path)

        if payload.get("kind") != SpanKind.CONVERSATION:
            return
        rounds_raw = payload.get("rounds", [])
        if not isinstance(rounds_raw, list):
            msg = (
                f"Conversation snapshot file {span_path} must store rounds as a JSON array under 'rounds'. "
                "Rewrite the snapshot so embedded llm_round and tool_call spans are grouped per round."
            )
            raise TypeError(msg)
        for round_raw in rounds_raw:
            if not isinstance(round_raw, dict):
                msg = f"Conversation snapshot file {span_path} contains a non-object round entry. Persist each round as a JSON object."
                raise TypeError(msg)
            llm_round = _deserialize_span(round_raw, path=span_path)
            self._store_span_from_disk(llm_round, path=span_path)
            tool_calls_raw = round_raw.get("tool_calls", [])
            if not isinstance(tool_calls_raw, list):
                msg = (
                    f"Conversation snapshot file {span_path} must store tool_calls as a JSON array inside each round. "
                    "Persist embedded tool_call spans under round['tool_calls']."
                )
                raise TypeError(msg)
            for tool_call_raw in tool_calls_raw:
                if not isinstance(tool_call_raw, dict):
                    msg = f"Conversation snapshot file {span_path} contains a non-object tool_call entry. Persist each tool_call as a JSON object."
                    raise TypeError(msg)
                self._store_span_from_disk(_deserialize_span(tool_call_raw, path=span_path), path=span_path)

    def _load_from_disk(self) -> None:
        for span_path in sorted(self._spans_dir.glob("*.json")):
            self._store_span_from_disk(_deserialize_span(_read_json_dict(span_path, context="Span snapshot"), path=span_path), path=span_path)

        for span_path in sorted(self._runs_dir.rglob("*.json")):
            self._load_tree_span_file(span_path)

        for document_path in sorted(self._documents_dir.rglob("*.json")):
            document = _deserialize_document(_read_json_dict(document_path, context="Document snapshot"), path=document_path)
            if document.document_sha256 in self._documents:
                continue
            self._documents[document.document_sha256] = document
            self._document_paths[document.document_sha256] = document_path

        if not self._logs_path.exists():
            return

        with self._logs_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    msg = (
                        f"Log snapshot file {self._logs_path} contains invalid JSON on line {line_number}. "
                        "Recreate the snapshot or repair the corrupted log entry."
                    )
                    raise ValueError(msg) from exc
                if not isinstance(value, dict):
                    msg = f"Log snapshot file {self._logs_path} contains a non-object line at {line_number}. Store each log entry as one JSON object per line."
                    raise TypeError(msg)
                self._logs.append(_deserialize_log(value, path=self._logs_path))

    def _span_path(self, span_id: UUID) -> Path:
        return self._spans_dir / f"{span_id}.json"

    def _document_path(self, document_sha256: str, document_type: str) -> Path:
        return self._documents_dir / document_type / f"{document_sha256}.json"

    def _blob_content_path(self, content_sha256: str) -> Path:
        return self._blobs_dir / content_sha256

    def _save_span_sync(self, span: SpanRecord) -> None:
        self._ensure_writable("insert spans")
        existing = self._spans.get(span.span_id)
        if existing is not None and existing.version >= span.version:
            return
        path = self._span_path(span.span_id)
        _atomic_write(
            path,
            json.dumps(_serialize_record(span), indent=2, sort_keys=True, default=_json_default),
        )
        self._spans[span.span_id] = span
        self._span_paths[span.span_id] = path

    def _save_document_sync(self, record: DocumentRecord) -> None:
        self._ensure_writable("save documents")
        if record.document_sha256 in self._documents:
            return
        path = self._document_path(record.document_sha256, record.document_type)
        _atomic_write(
            path,
            json.dumps(_serialize_record(record), indent=2, sort_keys=True, default=_json_default),
        )
        self._documents[record.document_sha256] = record
        self._document_paths[record.document_sha256] = path

    def _save_blob_sync(self, blob: _BlobRecord) -> None:
        self._ensure_writable("save blobs")
        content_path = self._blob_content_path(blob.content_sha256)
        if content_path.exists():
            return
        _atomic_write(content_path, blob.content)

    def _save_document_batch_sync(self, records: list[DocumentRecord]) -> None:
        for record in records:
            self._save_document_sync(record)

    def _save_blob_batch_sync(self, blobs: list[_BlobRecord]) -> None:
        for blob in blobs:
            self._save_blob_sync(blob)

    def _save_logs_batch_sync(self, logs: list[LogRecord]) -> None:
        self._ensure_writable("save logs")
        if not logs:
            return
        self._logs_path.parent.mkdir(parents=True, exist_ok=True)
        self._logs.extend(logs)
        content = "\n".join(json.dumps(_serialize_record(log), sort_keys=True, default=_json_default) for log in logs)
        if content:
            content += "\n"
        with self._logs_path.open("a", encoding="utf-8") as handle:
            handle.write(content)

    def _update_document_summary_sync(self, document_sha256: str, summary: str) -> None:
        self._ensure_writable("update document summaries")
        existing = self._documents.get(document_sha256)
        if existing is None:
            return
        updated = replace(existing, summary=summary)
        path = self._document_path(updated.document_sha256, updated.document_type)
        _atomic_write(
            path,
            json.dumps(_serialize_record(updated), indent=2, sort_keys=True, default=_json_default),
        )
        self._documents[updated.document_sha256] = updated
        self._document_paths[updated.document_sha256] = path

    def _root_deployment_spans(self) -> list[SpanRecord]:
        matches = [span for span in self._spans.values() if span.kind == SpanKind.DEPLOYMENT and span.deployment_id == span.root_deployment_id]
        return sorted(matches, key=deployment_sort_key)

    def _tree_children_map(self, root_deployment_id: UUID) -> dict[UUID, list[SpanRecord]]:
        children_map: dict[UUID, list[SpanRecord]] = {}
        for span in self._spans.values():
            if span.root_deployment_id != root_deployment_id or span.parent_span_id is None:
                continue
            children_map.setdefault(span.parent_span_id, []).append(span)
        for parent_span_id, children in children_map.items():
            children_map[parent_span_id] = sorted(children, key=tree_child_sort_key)
        return children_map

    def _span_payload(self, span: SpanRecord) -> dict[str, Any]:
        return _serialize_record(span)

    def _conversation_payload(self, span: SpanRecord, children_map: dict[UUID, list[SpanRecord]]) -> dict[str, Any]:
        payload = self._span_payload(span)
        rounds: list[dict[str, Any]] = []
        tool_calls_by_round: dict[int, list[SpanRecord]] = {}
        for child in children_map.get(span.span_id, []):
            if child.kind == SpanKind.TOOL_CALL:
                meta = parse_json_object(child.meta_json, context=f"Span {child.span_id}", field_name="meta_json")
                round_index = meta.get("round_index", 0)
                if not isinstance(round_index, int):
                    logger.warning(
                        "TOOL_CALL span %s has non-integer round_index=%r in meta_json. "
                        "Set round_index to an integer in the span's meta_json. Defaulting to 0.",
                        child.span_id,
                        round_index,
                    )
                    round_index = 0
                tool_calls_by_round.setdefault(round_index, []).append(child)
        for child in children_map.get(span.span_id, []):
            if child.kind != SpanKind.LLM_ROUND:
                continue
            round_payload = self._span_payload(child)
            round_meta = parse_json_object(child.meta_json, context=f"Span {child.span_id}", field_name="meta_json")
            round_index = round_meta.get("round_index", 0)
            if not isinstance(round_index, int):
                logger.warning(
                    "LLM_ROUND span %s has non-integer round_index=%r in meta_json. Set round_index to an integer in the span's meta_json. Defaulting to 0.",
                    child.span_id,
                    round_index,
                )
                round_index = 0
            tool_call_payloads = [self._span_payload(tool_call) for tool_call in tool_calls_by_round.get(round_index, [])]
            round_payload["tool_calls"] = tool_call_payloads
            rounds.append(round_payload)
        payload["rounds"] = rounds
        return payload

    def _write_span_tree(
        self,
        span: SpanRecord,
        *,
        parent_dir: Path,
        sibling_index: int,
        children_map: dict[UUID, list[SpanRecord]],
        is_root_deployment: bool = False,
    ) -> None:
        filename = "deployment.json" if is_root_deployment else span_filename(span.kind, span.name, span.span_id, sibling_index)
        file_path = parent_dir / filename
        payload = self._conversation_payload(span, children_map) if span.kind == SpanKind.CONVERSATION else self._span_payload(span)
        _atomic_write(file_path, json.dumps(payload, indent=2, sort_keys=True, default=_json_default))
        self._span_paths[span.span_id] = file_path

        # LLM_ROUND and TOOL_CALL children of CONVERSATION spans are embedded in the conversation JSON payload,
        # so they don't need separate files. Under any other parent kind, write them as regular child files.
        embedded_kinds = {SpanKind.LLM_ROUND, SpanKind.TOOL_CALL} if span.kind == SpanKind.CONVERSATION else set()
        child_spans = [child for child in children_map.get(span.span_id, []) if child.kind not in embedded_kinds]
        if not child_spans:
            return
        child_dir = parent_dir if is_root_deployment else file_path.with_suffix("")
        if not is_root_deployment:
            child_dir.mkdir(parents=True, exist_ok=True)
        for index, child in enumerate(child_spans, start=1):
            self._write_span_tree(
                child,
                parent_dir=child_dir,
                sibling_index=index,
                children_map=children_map,
            )

    def _remove_tree_directory(self) -> None:
        self._ensure_writable("rewrite run tree")
        if self._runs_dir.exists():
            shutil.rmtree(self._runs_dir)
        self._runs_dir.mkdir(parents=True, exist_ok=True)

    def _remove_staging_spans(self) -> None:
        self._ensure_writable("remove staging spans")
        for span_path in self._spans_dir.glob("*.json"):
            span_path.unlink()
        with contextlib.suppress(OSError):
            self._spans_dir.rmdir()

    def _reorganize_to_tree(self) -> None:
        self._remove_tree_directory()
        for root_span in self._root_deployment_spans():
            run_dir = self._runs_dir / run_directory_name(root_span.started_at, root_span.name, root_span.span_id)
            run_dir.mkdir(parents=True, exist_ok=True)
            children_map = self._tree_children_map(root_span.root_deployment_id)
            self._write_span_tree(
                root_span,
                parent_dir=run_dir,
                sibling_index=0,
                children_map=children_map,
                is_root_deployment=True,
            )
        self._remove_staging_spans()

    def _flush_sync(self) -> None:
        if self._read_only:
            return
        self._reorganize_to_tree()

    def _shutdown_sync(self) -> None:
        if self._read_only:
            return
        self._reorganize_to_tree()

    def _ensure_writable(self, action: str) -> None:
        if self._read_only:
            raise PermissionError(
                f"FilesystemDatabase at {self._base_path} is read-only and cannot {action}. "
                "Open the snapshot without read_only=True when you need write access."
            )

    def _get_span_sync(self, span_id: UUID) -> SpanRecord | None:
        return self._spans.get(span_id)

    def _get_child_spans_sync(self, parent_span_id: UUID) -> list[SpanRecord]:
        matches = [span for span in self._spans.values() if span.parent_span_id == parent_span_id]
        return sorted(matches, key=child_span_sort_key)

    def _get_deployment_tree_sync(self, root_deployment_id: UUID) -> list[SpanRecord]:
        matches = [span for span in self._spans.values() if span.root_deployment_id == root_deployment_id]
        return sorted(matches, key=span_sort_key)

    def _get_deployment_by_run_id_sync(self, run_id: str) -> SpanRecord | None:
        matches = [span for span in self._spans.values() if span.kind == SpanKind.DEPLOYMENT and span.run_id == run_id]
        if not matches:
            return None
        return max(matches, key=deployment_sort_key)

    def _list_deployments_sync(self, limit: int, status: str | None, root_only: bool) -> list[SpanRecord]:
        if limit <= 0:
            return []
        matches = [span for span in self._spans.values() if span.kind == SpanKind.DEPLOYMENT]
        if root_only:
            matches = [span for span in matches if span.span_id == span.root_deployment_id]
        if status is not None:
            matches = [span for span in matches if span.status == status]
        return sorted(matches, key=deployment_sort_key, reverse=True)[:limit]

    def _list_deployments_by_run_id_sync(self, run_id: str) -> list[SpanRecord]:
        matches = [span for span in self._spans.values() if span.kind == SpanKind.DEPLOYMENT and span.run_id == run_id]
        return sorted(matches, key=deployment_sort_key, reverse=True)

    def _get_cached_completion_sync(self, cache_key: str, max_age: timedelta | None) -> SpanRecord | None:
        now = datetime.now(UTC)
        matches: list[SpanRecord] = []
        for span in self._spans.values():
            if span.cache_key != cache_key:
                continue
            if span.status != SpanStatus.COMPLETED:
                continue
            if max_age is not None:
                if span.ended_at is None:
                    continue
                if now - span.ended_at > max_age:
                    continue
            matches.append(span)
        if not matches:
            return None
        return max(matches, key=lambda span: (span.ended_at or span.started_at, span.version, str(span.span_id)))

    def _get_deployment_cost_totals_sync(self, root_deployment_id: UUID) -> CostTotals:
        return aggregate_cost_totals(
            (span.kind, span.cost_usd, span.metrics_json, f"Span {span.span_id}")
            for span in self._spans.values()
            if span.root_deployment_id == root_deployment_id
        )

    def _get_deployment_span_count_sync(self, root_deployment_id: UUID, kinds: list[str] | None) -> int:
        if kinds == []:
            return 0
        allowed_kinds = set(kinds) if kinds is not None else None
        count = 0
        for span in self._spans.values():
            if span.root_deployment_id != root_deployment_id:
                continue
            if allowed_kinds is not None and span.kind not in allowed_kinds:
                continue
            count += 1
        return count

    def _get_spans_referencing_document_sync(self, document_sha256: str, kinds: list[str] | None) -> list[SpanRecord]:
        if kinds == []:
            return []
        allowed_kinds = set(kinds) if kinds is not None else None
        matches: list[SpanRecord] = []
        for span in self._spans.values():
            if allowed_kinds is not None and span.kind not in allowed_kinds:
                continue
            if document_sha256 in span.input_document_shas:
                matches.append(span)
                continue
            if document_sha256 in span.output_document_shas:
                matches.append(span)
                continue
            if document_sha256 in span.input_blob_shas or document_sha256 in span.output_blob_shas:
                matches.append(span)
        return sorted(matches, key=span_sort_key)

    def _get_document_sync(self, document_sha256: str) -> DocumentRecord | None:
        return self._documents.get(document_sha256)

    def _get_documents_batch_sync(self, sha256s: list[str]) -> dict[str, DocumentRecord]:
        return {sha: self._documents[sha] for sha in sha256s if sha in self._documents}

    def _get_blob_sync(self, content_sha256: str) -> _BlobRecord | None:
        path = self._blob_content_path(content_sha256)
        if not path.exists():
            return None
        return _BlobRecord(
            content_sha256=content_sha256,
            content=path.read_bytes(),
        )

    def _get_blobs_batch_sync(self, content_sha256s: list[str]) -> dict[str, _BlobRecord]:
        return {content_sha256: blob for content_sha256 in content_sha256s if (blob := self._get_blob_sync(content_sha256)) is not None}

    def _get_document_with_content_sync(self, document_sha256: str) -> HydratedDocument | None:
        record = self._documents.get(document_sha256)
        if record is None:
            return None
        blob = self._get_blob_sync(record.content_sha256)
        if blob is None:
            return None
        blobs = self._get_blobs_batch_sync(list(record.attachment_content_sha256s))
        attachment_contents = _attachment_contents_for_record(record, blobs)
        return HydratedDocument(record=record, content=blob.content, attachment_contents=attachment_contents)

    def _get_all_document_shas_for_tree_sync(self, root_deployment_id: UUID) -> set[str]:
        shas: set[str] = set()
        for span in self._spans.values():
            if span.root_deployment_id != root_deployment_id:
                continue
            shas.update(span.input_document_shas)
            shas.update(span.output_document_shas)
        return shas

    def _get_span_logs_sync(self, span_id: UUID, level: str | None, category: str | None) -> list[LogRecord]:
        return sorted(
            (log for log in self._logs if log.span_id == span_id and (level is None or log.level == level) and (category is None or log.category == category)),
            key=lambda log: (log.sequence_no, log.timestamp, str(log.span_id)),
        )

    def _get_deployment_logs_sync(self, deployment_id: UUID, level: str | None, category: str | None) -> list[LogRecord]:
        return sorted(
            (
                log
                for log in self._logs
                if log.deployment_id == deployment_id and (level is None or log.level == level) and (category is None or log.category == category)
            ),
            key=log_sort_key,
        )

    def _get_deployment_logs_batch_sync(self, deployment_ids: list[UUID], level: str | None, category: str | None) -> list[LogRecord]:
        allowed_ids = set(deployment_ids)
        return sorted(
            (
                log
                for log in self._logs
                if log.deployment_id in allowed_ids and (level is None or log.level == level) and (category is None or log.category == category)
            ),
            key=log_sort_key,
        )

    def _find_documents_by_name_sync(self, names: list[str], document_type: str | None) -> dict[str, DocumentRecord]:
        if not names:
            return {}
        name_set = set(names)
        found: dict[str, DocumentRecord] = {}
        for record in self._documents.values():
            if record.name not in name_set:
                continue
            if document_type is not None and record.document_type != document_type:
                continue
            existing = found.get(record.name)
            if existing is None or record.document_sha256 > existing.document_sha256:
                found[record.name] = record
        return found

    async def insert_span(self, span: SpanRecord) -> None:
        await self._run_write(self._save_span_sync, span)

    async def save_document(self, record: DocumentRecord) -> None:
        await self._run_write(self._save_document_sync, record)

    async def save_document_batch(self, records: list[DocumentRecord]) -> None:
        await self._run_write(self._save_document_batch_sync, records)

    async def save_blob(self, blob: _BlobRecord) -> None:
        await self._run_write(self._save_blob_sync, blob)

    async def save_blob_batch(self, blobs: list[_BlobRecord]) -> None:
        await self._run_write(self._save_blob_batch_sync, blobs)

    async def save_logs_batch(self, logs: list[LogRecord]) -> None:
        await self._run_write(self._save_logs_batch_sync, logs)

    async def update_document_summary(self, document_sha256: str, summary: str) -> None:
        await self._run_write(self._update_document_summary_sync, document_sha256, summary)

    async def flush(self) -> None:
        await self._run_write(self._flush_sync)

    async def shutdown(self) -> None:
        await self._run_write(self._shutdown_sync)

    async def get_span(self, span_id: UUID) -> SpanRecord | None:
        return await self._run(self._get_span_sync, span_id)

    async def get_child_spans(self, parent_span_id: UUID) -> list[SpanRecord]:
        return await self._run(self._get_child_spans_sync, parent_span_id)

    async def get_deployment_tree(self, root_deployment_id: UUID) -> list[SpanRecord]:
        return await self._run(self._get_deployment_tree_sync, root_deployment_id)

    async def get_deployment_by_run_id(self, run_id: str) -> SpanRecord | None:
        return await self._run(self._get_deployment_by_run_id_sync, run_id)

    async def list_deployments(
        self,
        limit: int,
        *,
        status: str | None = None,
        root_only: bool = False,
    ) -> list[SpanRecord]:
        return await self._run(self._list_deployments_sync, limit, status, root_only)

    async def list_deployments_by_run_id(self, run_id: str) -> list[SpanRecord]:
        return await self._run(self._list_deployments_by_run_id_sync, run_id)

    async def get_cached_completion(
        self,
        cache_key: str,
        *,
        max_age: timedelta | None = None,
    ) -> SpanRecord | None:
        return await self._run(self._get_cached_completion_sync, cache_key, max_age)

    async def get_deployment_cost_totals(self, root_deployment_id: UUID) -> CostTotals:
        return await self._run(self._get_deployment_cost_totals_sync, root_deployment_id)

    async def get_deployment_span_count(
        self,
        root_deployment_id: UUID,
        *,
        kinds: list[str] | None = None,
    ) -> int:
        return await self._run(self._get_deployment_span_count_sync, root_deployment_id, kinds)

    async def get_spans_referencing_document(
        self,
        document_sha256: str,
        *,
        kinds: list[str] | None = None,
    ) -> list[SpanRecord]:
        return await self._run(self._get_spans_referencing_document_sync, document_sha256, kinds)

    async def get_document(self, document_sha256: str) -> DocumentRecord | None:
        return await self._run(self._get_document_sync, document_sha256)

    async def get_documents_batch(self, sha256s: list[str]) -> dict[str, DocumentRecord]:
        return await self._run(self._get_documents_batch_sync, sha256s)

    async def get_document_with_content(
        self,
        document_sha256: str,
    ) -> HydratedDocument | None:
        return await self._run(self._get_document_with_content_sync, document_sha256)

    async def get_all_document_shas_for_tree(self, root_deployment_id: UUID) -> set[str]:
        return await self._run(self._get_all_document_shas_for_tree_sync, root_deployment_id)

    async def get_blob(self, content_sha256: str) -> _BlobRecord | None:
        return await self._run(self._get_blob_sync, content_sha256)

    async def get_blobs_batch(self, content_sha256s: list[str]) -> dict[str, _BlobRecord]:
        return await self._run(self._get_blobs_batch_sync, content_sha256s)

    async def get_span_logs(
        self,
        span_id: UUID,
        *,
        level: str | None = None,
        category: str | None = None,
    ) -> list[LogRecord]:
        return await self._run(self._get_span_logs_sync, span_id, level, category)

    async def get_deployment_logs(
        self,
        deployment_id: UUID,
        *,
        level: str | None = None,
        category: str | None = None,
    ) -> list[LogRecord]:
        return await self._run(self._get_deployment_logs_sync, deployment_id, level, category)

    async def get_deployment_logs_batch(
        self,
        deployment_ids: list[UUID],
        *,
        level: str | None = None,
        category: str | None = None,
    ) -> list[LogRecord]:
        return await self._run(self._get_deployment_logs_batch_sync, deployment_ids, level, category)

    async def find_documents_by_name(
        self,
        names: list[str],
        *,
        document_type: str | None = None,
    ) -> dict[str, DocumentRecord]:
        return await self._run(self._find_documents_by_name_sync, names, document_type)
