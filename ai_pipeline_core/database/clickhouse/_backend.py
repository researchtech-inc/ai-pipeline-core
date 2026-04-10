"""ClickHouse backend for the redesigned span/document/blob/log schema."""

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from clickhouse_connect.driver.asyncclient import AsyncClient
from clickhouse_connect.driver.exceptions import DatabaseError as ClickHouseDatabaseError

from ai_pipeline_core.database._documents import _attachment_contents_for_record
from ai_pipeline_core.database._serialization import (
    LOG_COLUMNS,
    SPAN_COLUMNS,
    decode_text,
    row_to_log,
    row_to_span,
    string_tuple,
)
from ai_pipeline_core.database._sorting import span_sort_key
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
from ai_pipeline_core.database.clickhouse._connection import get_async_clickhouse_client
from ai_pipeline_core.database.clickhouse._ddl import BLOBS_TABLE, DOCUMENTS_TABLE, LOGS_TABLE, SPANS_TABLE
from ai_pipeline_core.database.clickhouse._rows import (
    BLOB_COLUMNS,
    BLOB_SELECT_COLUMNS,
    DOCUMENT_COLUMNS,
    blob_to_row,
    document_to_row,
    log_to_row,
    row_to_blob,
    row_to_document,
    span_to_row,
)
from ai_pipeline_core.settings import Settings

__all__ = [
    "ClickHouseDatabase",
]

logger = logging.getLogger(__name__)

_FAILURE_THRESHOLD = 3
_RECONNECT_INTERVAL_SEC = 60
_TOPOLOGY_SPAN_COLUMNS = (
    "span_id",
    "parent_span_id",
    "deployment_id",
    "root_deployment_id",
    "run_id",
    "deployment_name",
    "kind",
    "name",
    "description",
    "status",
    "sequence_no",
    "started_at",
    "ended_at",
    "version",
    "cache_key",
    "previous_conversation_id",
    "cost_usd",
    "error_type",
    "'' AS error_message",
    "input_document_shas",
    "output_document_shas",
    "target",
    "'' AS receiver_json",
    "'' AS input_json",
    "'' AS output_json",
    "'' AS error_json",
    "'' AS meta_json",
    "'' AS metrics_json",
    "input_blob_shas",
    "output_blob_shas",
)


def _root_latest_spans_query(select_sql: str, *, extra_where: str = "") -> str:
    return (
        "WITH latest AS ("
        f"SELECT span_id, max(version) AS version FROM {SPANS_TABLE} "
        "WHERE root_deployment_id = {root_deployment_id:UUID} "
        "GROUP BY span_id"
        ") "
        f"SELECT {select_sql} "
        f"FROM {SPANS_TABLE} AS s "
        "INNER JOIN latest USING (span_id, version) "
        "WHERE s.root_deployment_id = {root_deployment_id:UUID}"
        f"{extra_where}"
    )


class ClickHouseDatabase:
    """ClickHouse backend implementing the redesigned span protocols."""

    supports_remote = True

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or Settings()
        self._client: AsyncClient | None = None
        self._consecutive_failures = 0
        self._circuit_open = False
        self._last_reconnect_attempt = 0.0

    async def _ensure_client(self) -> AsyncClient:
        now = time.monotonic()
        if self._client is not None:
            return self._client
        if self._circuit_open and now - self._last_reconnect_attempt < _RECONNECT_INTERVAL_SEC:
            raise ConnectionError("ClickHouse circuit breaker is open. Wait before retrying or restore ClickHouse connectivity.")
        self._last_reconnect_attempt = now
        try:
            self._client = await get_async_clickhouse_client(self._settings)
        except ClickHouseDatabaseError, ConnectionError, OSError:
            await self._record_failure()
            raise
        self._record_success()
        return self._client

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._circuit_open = False

    async def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._client is not None:
            await self._client.close()
            self._client = None
        if self._consecutive_failures >= _FAILURE_THRESHOLD:
            self._circuit_open = True
            logger.warning("ClickHouse circuit breaker opened after %d consecutive failures", self._consecutive_failures)

    async def _query(self, sql: str, *, parameters: dict[str, Any] | None = None) -> Any:
        client = await self._ensure_client()
        try:
            result = await client.query(sql, parameters=parameters)
        except ClickHouseDatabaseError, ConnectionError, OSError:
            await self._record_failure()
            raise
        self._record_success()
        return result

    async def _insert(self, table: str, rows: list[list[Any]], *, column_names: tuple[str, ...]) -> None:
        if not rows:
            return
        client = await self._ensure_client()
        try:
            await client.insert(table, rows, column_names=list(column_names))
        except ClickHouseDatabaseError, ConnectionError, OSError:
            await self._record_failure()
            raise
        self._record_success()

    async def _command(self, sql: str, *, parameters: dict[str, Any] | None = None) -> Any:
        client = await self._ensure_client()
        try:
            result = await client.command(sql, parameters=parameters)
        except ClickHouseDatabaseError, ConnectionError, OSError:
            await self._record_failure()
            raise
        self._record_success()
        return result

    async def insert_span(self, span: SpanRecord) -> None:
        await self._insert(SPANS_TABLE, [span_to_row(span)], column_names=SPAN_COLUMNS)

    async def save_document(self, record: DocumentRecord) -> None:
        await self._insert(DOCUMENTS_TABLE, [document_to_row(record)], column_names=DOCUMENT_COLUMNS)

    async def save_document_batch(self, records: list[DocumentRecord]) -> None:
        await self._insert(
            DOCUMENTS_TABLE,
            [document_to_row(record) for record in records],
            column_names=DOCUMENT_COLUMNS,
        )

    async def save_blob(self, blob: _BlobRecord) -> None:
        await self._insert(BLOBS_TABLE, [blob_to_row(blob)], column_names=BLOB_COLUMNS)

    async def save_blob_batch(self, blobs: list[_BlobRecord]) -> None:
        await self._insert(
            BLOBS_TABLE,
            [blob_to_row(blob) for blob in blobs],
            column_names=BLOB_COLUMNS,
        )

    async def save_logs_batch(self, logs: list[LogRecord]) -> None:
        await self._insert(
            LOGS_TABLE,
            [log_to_row(log) for log in logs],
            column_names=LOG_COLUMNS,
        )

    async def update_document_summary(self, document_sha256: str, summary: str) -> None:
        await self._command(
            f"INSERT INTO {DOCUMENTS_TABLE} ({', '.join(DOCUMENT_COLUMNS)}) "
            "SELECT document_sha256, content_sha256, document_type, name, "
            "description, mime_type, size_bytes, {summary:String}, "
            "derived_from, triggered_by, "
            "`attachments.name`, `attachments.description`, "
            "`attachments.content_sha256`, `attachments.mime_type`, "
            "`attachments.size_bytes`, "
            "publicly_visible "
            f"FROM {DOCUMENTS_TABLE} FINAL WHERE document_sha256 = {{document_sha256:String}}",
            parameters={"summary": summary, "document_sha256": document_sha256},
        )

    async def flush(self) -> None:
        return None

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def get_span(self, span_id: UUID) -> SpanRecord | None:
        result = await self._query(
            f"SELECT {', '.join(SPAN_COLUMNS)} FROM {SPANS_TABLE} WHERE span_id = {{span_id:UUID}} ORDER BY version DESC LIMIT 1",
            parameters={"span_id": span_id},
        )
        if not result.result_rows:
            return None
        return row_to_span(tuple(result.result_rows[0]))

    async def get_child_spans(self, parent_span_id: UUID) -> list[SpanRecord]:
        result = await self._query(
            f"SELECT {', '.join(SPAN_COLUMNS)} FROM ("
            f"SELECT * FROM {SPANS_TABLE} "
            "WHERE parent_span_id = {parent_span_id:UUID} "
            "ORDER BY span_id, version DESC LIMIT 1 BY span_id"
            ") ORDER BY sequence_no, started_at, span_id",
            parameters={"parent_span_id": parent_span_id},
        )
        return [row_to_span(tuple(row)) for row in result.result_rows]

    async def get_deployment_tree(self, root_deployment_id: UUID) -> list[SpanRecord]:
        result = await self._query(
            _root_latest_spans_query(", ".join(SPAN_COLUMNS)),
            parameters={"root_deployment_id": root_deployment_id},
        )
        return sorted(
            (row_to_span(tuple(row)) for row in result.result_rows),
            key=span_sort_key,
        )

    async def get_deployment_tree_topology(self, root_deployment_id: UUID) -> list[SpanRecord]:
        """Return tree structure only; payload JSON fields are blanked empty strings.

        Callers must not read ``input_json``, ``output_json``, ``meta_json``,
        ``metrics_json``, ``receiver_json``, ``error_json``, or
        ``error_message`` from these rows. Use ``get_span()`` or
        ``get_deployment_tree()`` when payload content is required.
        """
        result = await self._query(
            _root_latest_spans_query(", ".join(_TOPOLOGY_SPAN_COLUMNS)),
            parameters={"root_deployment_id": root_deployment_id},
        )
        return sorted(
            (row_to_span(tuple(row)) for row in result.result_rows),
            key=span_sort_key,
        )

    async def get_deployment_latest_activity(self, root_deployment_id: UUID) -> datetime | None:
        return await self.latest_span_activity_for_deployment(root_deployment_id)

    async def get_deployment_by_run_id(self, run_id: str) -> SpanRecord | None:
        result = await self._query(
            f"SELECT {', '.join(SPAN_COLUMNS)} FROM ("
            f"SELECT * FROM {SPANS_TABLE} "
            "WHERE run_id = {run_id:String} AND kind = {kind:String} "
            "ORDER BY span_id, version DESC LIMIT 1 BY span_id"
            ") ORDER BY started_at DESC, span_id DESC LIMIT 1",
            parameters={"run_id": run_id, "kind": SpanKind.DEPLOYMENT},
        )
        if not result.result_rows:
            return None
        return row_to_span(tuple(result.result_rows[0]))

    async def list_deployments(
        self,
        limit: int,
        *,
        status: str | None = None,
        root_only: bool = False,
    ) -> list[SpanRecord]:
        if limit <= 0:
            return []
        parameters: dict[str, Any] = {"kind": SpanKind.DEPLOYMENT, "limit": limit}
        # kind is immutable → inner filter; status is mutable → outer filter
        outer_filters: list[str] = []
        if root_only:
            outer_filters.append("span_id = root_deployment_id")
        if status is not None:
            outer_filters.append("status = {status:String}")
            parameters["status"] = status
        outer_where = f"WHERE {' AND '.join(outer_filters)} " if outer_filters else ""
        result = await self._query(
            f"SELECT {', '.join(SPAN_COLUMNS)} FROM ("
            f"SELECT * FROM {SPANS_TABLE} "
            "WHERE kind = {kind:String} "
            "ORDER BY span_id, version DESC LIMIT 1 BY span_id"
            f") {outer_where}"
            "ORDER BY started_at DESC, span_id DESC LIMIT {limit:UInt64}",
            parameters=parameters,
        )
        return [row_to_span(tuple(row)) for row in result.result_rows]

    async def list_deployments_by_run_id(self, run_id: str) -> list[SpanRecord]:
        result = await self._query(
            f"SELECT {', '.join(SPAN_COLUMNS)} FROM ("
            f"SELECT * FROM {SPANS_TABLE} "
            "WHERE run_id = {run_id:String} AND kind = {kind:String} "
            "ORDER BY span_id, version DESC LIMIT 1 BY span_id"
            ") ORDER BY started_at DESC, span_id DESC",
            parameters={"run_id": run_id, "kind": SpanKind.DEPLOYMENT},
        )
        return [row_to_span(tuple(row)) for row in result.result_rows]

    async def list_running_deployment_roots(
        self,
        *,
        limit: int = 1000,
    ) -> list[SpanRecord]:
        if limit <= 0:
            return []
        result = await self._query(
            f"SELECT {', '.join(SPAN_COLUMNS)} FROM ("
            f"SELECT * FROM {SPANS_TABLE} "
            "WHERE kind = {kind:String} "
            "ORDER BY span_id, version DESC LIMIT 1 BY span_id"
            ") WHERE span_id = root_deployment_id "
            "AND status = {status:String} "
            "ORDER BY started_at ASC, span_id ASC LIMIT {limit:UInt64}",
            parameters={
                "kind": SpanKind.DEPLOYMENT,
                "status": SpanStatus.RUNNING,
                "limit": limit,
            },
        )
        return [row_to_span(tuple(row)) for row in result.result_rows]

    async def latest_span_activity_for_deployment(self, root_deployment_id: UUID) -> datetime | None:
        result = await self._query(
            _root_latest_spans_query("max(coalesce(s.ended_at, s.started_at))"),
            parameters={"root_deployment_id": root_deployment_id},
        )
        if not result.result_rows:
            return None
        value = result.result_rows[0][0]
        if not isinstance(value, datetime):
            return None
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    async def get_cached_completion(
        self,
        cache_key: str,
        *,
        max_age: timedelta | None = None,
    ) -> SpanRecord | None:
        # Mutable fields (status, ended_at) are filtered on the outer query
        # after deduplication to avoid stale-version matches.
        outer_filters = ["status = {status:String}"]
        parameters: dict[str, Any] = {
            "cache_key": cache_key,
            "status": SpanStatus.COMPLETED,
        }
        if max_age is not None:
            outer_filters.append("ended_at IS NOT NULL")
            outer_filters.append("ended_at >= {min_ended_at:DateTime64(3)}")
            parameters["min_ended_at"] = datetime.now(UTC) - max_age
        result = await self._query(
            f"SELECT {', '.join(SPAN_COLUMNS)} FROM ("
            f"SELECT * FROM {SPANS_TABLE} "
            "WHERE cache_key = {cache_key:String} "
            "ORDER BY span_id, version DESC LIMIT 1 BY span_id"
            f") WHERE {' AND '.join(outer_filters)} "
            "ORDER BY ended_at DESC, span_id DESC LIMIT 1",
            parameters=parameters,
        )
        if not result.result_rows:
            return None
        return row_to_span(tuple(result.result_rows[0]))

    async def get_deployment_cost_totals(self, root_deployment_id: UUID) -> CostTotals:
        result = await self._query(
            _root_latest_spans_query("s.kind, s.cost_usd, s.metrics_json"),
            parameters={"root_deployment_id": root_deployment_id},
        )
        return aggregate_cost_totals(
            (decode_text(kind), float(span_cost_usd), decode_text(metrics_json), f"Span tree {root_deployment_id}")
            for kind, span_cost_usd, metrics_json in result.result_rows
        )

    async def get_deployment_span_count(
        self,
        root_deployment_id: UUID,
        *,
        kinds: list[str] | None = None,
    ) -> int:
        if kinds == []:
            return 0
        parameters: dict[str, Any] = {
            "root_deployment_id": root_deployment_id,
            "kinds": kinds or [],
        }
        result = await self._query(
            _root_latest_spans_query(
                "count()",
                extra_where=" AND (length({kinds:Array(String)}) = 0 OR s.kind IN {kinds:Array(String)})",
            ),
            parameters=parameters,
        )
        return int(result.result_rows[0][0]) if result.result_rows else 0

    async def get_spans_referencing_document(
        self,
        document_sha256: str,
        *,
        kinds: list[str] | None = None,
    ) -> list[SpanRecord]:
        if kinds == []:
            return []
        parameters: dict[str, Any] = {
            "document_sha256": document_sha256,
            "kinds": kinds or [],
        }
        result = await self._query(
            "WITH latest AS ("
            f"SELECT span_id, max(version) AS version FROM {SPANS_TABLE} "
            "WHERE ("
            "has(input_document_shas, {document_sha256:String}) "
            "OR has(output_document_shas, {document_sha256:String}) "
            "OR has(input_blob_shas, {document_sha256:String}) "
            "OR has(output_blob_shas, {document_sha256:String})"
            ") "
            "AND (length({kinds:Array(String)}) = 0 OR kind IN {kinds:Array(String)}) "
            "GROUP BY span_id"
            ") "
            f"SELECT {', '.join(_TOPOLOGY_SPAN_COLUMNS)} "
            f"FROM {SPANS_TABLE} AS s "
            "INNER JOIN latest USING (span_id, version) "
            "WHERE ("
            "has(s.input_document_shas, {document_sha256:String}) "
            "OR has(s.output_document_shas, {document_sha256:String}) "
            "OR has(s.input_blob_shas, {document_sha256:String}) "
            "OR has(s.output_blob_shas, {document_sha256:String})"
            ") "
            "AND (length({kinds:Array(String)}) = 0 OR s.kind IN {kinds:Array(String)})",
            parameters=parameters,
        )
        return sorted(
            (row_to_span(tuple(row)) for row in result.result_rows),
            key=span_sort_key,
        )

    async def get_document(self, document_sha256: str) -> DocumentRecord | None:
        result = await self._query(
            f"SELECT {', '.join(DOCUMENT_COLUMNS)} FROM {DOCUMENTS_TABLE} FINAL WHERE document_sha256 = {{document_sha256:String}}",
            parameters={"document_sha256": document_sha256},
        )
        if not result.result_rows:
            return None
        return row_to_document(tuple(result.result_rows[0]))

    async def get_documents_batch(self, sha256s: list[str]) -> dict[str, DocumentRecord]:
        unique_shas = list(dict.fromkeys(sha256s))
        if not unique_shas:
            return {}
        result = await self._query(
            f"SELECT {', '.join(DOCUMENT_COLUMNS)} FROM {DOCUMENTS_TABLE} FINAL WHERE document_sha256 IN {{document_sha256s:Array(String)}}",
            parameters={"document_sha256s": unique_shas},
        )
        return {document.document_sha256: document for document in (row_to_document(tuple(row)) for row in result.result_rows)}

    async def get_document_with_content(self, document_sha256: str) -> HydratedDocument | None:
        record = await self.get_document(document_sha256)
        if record is None:
            return None

        needed_shas = [record.content_sha256, *record.attachment_content_sha256s]
        blobs = await self.get_blobs_batch(needed_shas)

        primary_blob = blobs.get(record.content_sha256)
        if primary_blob is None:
            return None

        attachment_contents = _attachment_contents_for_record(record, blobs)
        return HydratedDocument(
            record=record,
            content=primary_blob.content,
            attachment_contents=attachment_contents,
        )

    async def get_all_document_shas_for_tree(self, root_deployment_id: UUID) -> set[str]:
        result = await self._query(
            _root_latest_spans_query(
                "arrayDistinct(arrayConcat(groupUniqArrayArray(s.input_document_shas), groupUniqArrayArray(s.output_document_shas))) AS shas"
            ),
            parameters={"root_deployment_id": root_deployment_id},
        )
        if not result.result_rows:
            return set()
        return set(string_tuple(result.result_rows[0][0]))

    async def get_blob(self, content_sha256: str) -> _BlobRecord | None:
        result = await self._query(
            f"SELECT {', '.join(BLOB_SELECT_COLUMNS)} FROM {BLOBS_TABLE} WHERE content_sha256 = {{content_sha256:String}} LIMIT 1",
            parameters={"content_sha256": content_sha256},
        )
        if not result.result_rows:
            return None
        return row_to_blob(tuple(result.result_rows[0]))

    async def get_blobs_batch(self, content_sha256s: list[str]) -> dict[str, _BlobRecord]:
        unique_shas = list(dict.fromkeys(content_sha256s))
        if not unique_shas:
            return {}
        result = await self._query(
            f"SELECT {', '.join(BLOB_SELECT_COLUMNS)} FROM {BLOBS_TABLE} WHERE content_sha256 IN {{content_sha256s:Array(String)}}",
            parameters={"content_sha256s": unique_shas},
        )
        return {blob.content_sha256: blob for blob in (row_to_blob(tuple(row)) for row in result.result_rows)}

    async def get_span_logs(
        self,
        span_id: UUID,
        *,
        level: str | None = None,
        category: str | None = None,
    ) -> list[LogRecord]:
        filters = ["span_id = {span_id:UUID}"]
        parameters: dict[str, Any] = {"span_id": span_id}
        if level is not None:
            filters.append("level = {level:String}")
            parameters["level"] = level
        if category is not None:
            filters.append("category = {category:String}")
            parameters["category"] = category
        result = await self._query(
            f"SELECT {', '.join(LOG_COLUMNS)} FROM {LOGS_TABLE} WHERE {' AND '.join(filters)} ORDER BY sequence_no, timestamp, span_id",
            parameters=parameters,
        )
        return [row_to_log(tuple(row)) for row in result.result_rows]

    async def get_deployment_logs(
        self,
        deployment_id: UUID,
        *,
        level: str | None = None,
        category: str | None = None,
    ) -> list[LogRecord]:
        filters = ["deployment_id = {deployment_id:UUID}"]
        parameters: dict[str, Any] = {"deployment_id": deployment_id}
        if level is not None:
            filters.append("level = {level:String}")
            parameters["level"] = level
        if category is not None:
            filters.append("category = {category:String}")
            parameters["category"] = category
        result = await self._query(
            f"SELECT {', '.join(LOG_COLUMNS)} FROM {LOGS_TABLE} WHERE {' AND '.join(filters)} ORDER BY timestamp, sequence_no, span_id",
            parameters=parameters,
        )
        return [row_to_log(tuple(row)) for row in result.result_rows]

    async def get_deployment_logs_batch(
        self,
        deployment_ids: list[UUID],
        *,
        level: str | None = None,
        category: str | None = None,
    ) -> list[LogRecord]:
        if not deployment_ids:
            return []
        filters = ["deployment_id IN {deployment_ids:Array(String)}"]
        parameters: dict[str, Any] = {"deployment_ids": [str(uid) for uid in deployment_ids]}
        if level is not None:
            filters.append("level = {level:String}")
            parameters["level"] = level
        if category is not None:
            filters.append("category = {category:String}")
            parameters["category"] = category
        result = await self._query(
            f"SELECT {', '.join(LOG_COLUMNS)} FROM {LOGS_TABLE} WHERE {' AND '.join(filters)} ORDER BY timestamp, sequence_no, span_id",
            parameters=parameters,
        )
        return [row_to_log(tuple(row)) for row in result.result_rows]

    async def find_documents_by_name(
        self,
        names: list[str],
        *,
        document_type: str | None = None,
    ) -> dict[str, DocumentRecord]:
        if not names:
            return {}
        unique_names = list(dict.fromkeys(names))
        filters = ["name IN {names:Array(String)}"]
        parameters: dict[str, Any] = {"names": unique_names}
        if document_type is not None:
            filters.append("document_type = {document_type:String}")
            parameters["document_type"] = document_type
        result = await self._query(
            f"SELECT {', '.join(DOCUMENT_COLUMNS)} FROM {DOCUMENTS_TABLE} FINAL WHERE {' AND '.join(filters)}",
            parameters=parameters,
        )
        found: dict[str, DocumentRecord] = {}
        for row in result.result_rows:
            record = row_to_document(tuple(row))
            existing = found.get(record.name)
            if existing is None or record.document_sha256 > existing.document_sha256:
                found[record.name] = record
        return found
