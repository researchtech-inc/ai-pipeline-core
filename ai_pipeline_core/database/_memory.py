"""In-memory backend for the span-based database schema."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

from ai_pipeline_core.database._documents import _attachment_contents_for_record
from ai_pipeline_core.database._sorting import (
    child_span_sort_key,
    deployment_sort_key,
    log_sort_key,
    span_sort_key,
)
from ai_pipeline_core.database._types import (
    CostTotals,
    DeploymentSummaryRecord,
    DocumentEventPage,
    DocumentEventRecord,
    DocumentProducerRecord,
    DocumentRecord,
    HydratedDocument,
    LogRecord,
    SpanKind,
    SpanRecord,
    SpanStatus,
    _BlobRecord,
    aggregate_cost_totals,
)

__all__ = ["_MemoryDatabase"]


class _MemoryDatabase:
    """Dict-based backend for tests covering the span schema."""

    supports_remote = False

    def __init__(self) -> None:
        self._spans: dict[UUID, SpanRecord] = {}
        self._documents: dict[str, DocumentRecord] = {}
        self._blobs: dict[str, _BlobRecord] = {}
        self._logs: list[LogRecord] = []

    async def insert_span(self, span: SpanRecord) -> None:
        existing = self._spans.get(span.span_id)
        if existing is None or span.version > existing.version:
            self._spans[span.span_id] = span

    async def save_document(self, record: DocumentRecord) -> None:
        if record.document_sha256 in self._documents:
            return
        self._documents[record.document_sha256] = record

    async def save_document_batch(self, records: list[DocumentRecord]) -> None:
        for record in records:
            await self.save_document(record)

    async def save_blob(self, blob: _BlobRecord) -> None:
        if blob.content_sha256 in self._blobs:
            return
        self._blobs[blob.content_sha256] = blob

    async def save_blob_batch(self, blobs: list[_BlobRecord]) -> None:
        for blob in blobs:
            await self.save_blob(blob)

    async def save_logs_batch(self, logs: list[LogRecord]) -> None:
        self._logs.extend(logs)

    async def update_document_summary(self, document_sha256: str, summary: str) -> None:
        existing = self._documents.get(document_sha256)
        if existing is None:
            return
        self._documents[document_sha256] = replace(existing, summary=summary)

    async def flush(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def get_span(self, span_id: UUID) -> SpanRecord | None:
        return self._spans.get(span_id)

    async def get_child_spans(self, parent_span_id: UUID) -> list[SpanRecord]:
        matches = [span for span in self._spans.values() if span.parent_span_id == parent_span_id]
        return sorted(matches, key=child_span_sort_key)

    async def get_deployment_tree(self, root_deployment_id: UUID) -> list[SpanRecord]:
        matches = [span for span in self._spans.values() if span.root_deployment_id == root_deployment_id]
        return sorted(matches, key=span_sort_key)

    async def get_deployment_tree_topology(self, root_deployment_id: UUID) -> list[SpanRecord]:
        return await self.get_deployment_tree(root_deployment_id)

    async def get_deployment_latest_activity(self, root_deployment_id: UUID) -> datetime | None:
        return await self.latest_span_activity_for_deployment(root_deployment_id)

    async def get_deployment_by_run_id(self, run_id: str) -> SpanRecord | None:
        matches = [span for span in self._spans.values() if span.kind == SpanKind.DEPLOYMENT and span.run_id == run_id]
        if not matches:
            return None
        return max(matches, key=deployment_sort_key)

    async def list_deployments(
        self,
        limit: int,
        *,
        status: str | None = None,
        root_only: bool = False,
        offset: int = 0,
    ) -> list[SpanRecord]:
        matches = [span for span in self._spans.values() if span.kind == SpanKind.DEPLOYMENT]
        if root_only:
            matches = [span for span in matches if span.span_id == span.root_deployment_id]
        if status is not None:
            matches = [span for span in matches if span.status == status]
        return sorted(matches, key=deployment_sort_key, reverse=True)[offset : offset + limit]

    async def list_deployments_by_run_id(self, run_id: str) -> list[SpanRecord]:
        matches = [span for span in self._spans.values() if span.kind == SpanKind.DEPLOYMENT and span.run_id == run_id]
        return sorted(matches, key=deployment_sort_key, reverse=True)

    async def list_running_deployment_roots(
        self,
        *,
        limit: int = 1000,
    ) -> list[SpanRecord]:
        if limit <= 0:
            return []
        matches = [
            span
            for span in self._spans.values()
            if span.kind == SpanKind.DEPLOYMENT and span.span_id == span.root_deployment_id and span.status == SpanStatus.RUNNING
        ]
        return sorted(matches, key=lambda span: (span.started_at, str(span.span_id)))[:limit]

    async def latest_span_activity_for_deployment(self, root_deployment_id: UUID) -> datetime | None:
        latest: datetime | None = None
        for span in self._spans.values():
            if span.root_deployment_id != root_deployment_id:
                continue
            activity = span.ended_at or span.started_at
            if latest is None or activity > latest:
                latest = activity
        return latest

    async def get_cached_completion(
        self,
        cache_key: str,
        *,
        max_age: timedelta | None = None,
    ) -> SpanRecord | None:
        now = datetime.now(UTC)
        matches: list[SpanRecord] = []
        for span in self._spans.values():
            if span.cache_key != cache_key or span.status != SpanStatus.COMPLETED:
                continue
            if max_age is not None and (span.ended_at is None or now - span.ended_at > max_age):
                continue
            matches.append(span)
        if not matches:
            return None
        return max(matches, key=lambda span: (span.ended_at or span.started_at, span.version, str(span.span_id)))

    async def get_deployment_cost_totals(self, root_deployment_id: UUID) -> CostTotals:
        return aggregate_cost_totals(
            (span.kind, span.cost_usd, span.metrics_json, f"Span {span.span_id}")
            for span in self._spans.values()
            if span.root_deployment_id == root_deployment_id
        )

    async def get_deployment_span_count(
        self,
        root_deployment_id: UUID,
        *,
        kinds: list[str] | None = None,
    ) -> int:
        allowed_kinds = set(kinds) if kinds is not None else None
        return sum(
            1 for span in self._spans.values() if span.root_deployment_id == root_deployment_id and (allowed_kinds is None or span.kind in allowed_kinds)
        )

    async def get_spans_referencing_document(
        self,
        document_sha256: str,
        *,
        kinds: list[str] | None = None,
    ) -> list[SpanRecord]:
        allowed_kinds = set(kinds) if kinds is not None else None
        matches: list[SpanRecord] = []
        for span in self._spans.values():
            if allowed_kinds is not None and span.kind not in allowed_kinds:
                continue
            if document_sha256 in span.input_document_shas or document_sha256 in span.output_document_shas:
                matches.append(span)
                continue
            if document_sha256 in span.input_blob_shas or document_sha256 in span.output_blob_shas:
                matches.append(span)
        return sorted(matches, key=span_sort_key)

    async def get_document(self, document_sha256: str) -> DocumentRecord | None:
        return self._documents.get(document_sha256)

    async def get_documents_batch(self, sha256s: list[str]) -> dict[str, DocumentRecord]:
        return {sha256: self._documents[sha256] for sha256 in sha256s if sha256 in self._documents}

    async def get_document_with_content(self, document_sha256: str) -> HydratedDocument | None:
        record = self._documents.get(document_sha256)
        if record is None:
            return None
        blob = self._blobs.get(record.content_sha256)
        if blob is None:
            return None
        attachment_contents = _attachment_contents_for_record(record, self._blobs)
        return HydratedDocument(record=record, content=blob.content, attachment_contents=attachment_contents)

    async def get_all_document_shas_for_tree(self, root_deployment_id: UUID) -> set[str]:
        shas: set[str] = set()
        for span in self._spans.values():
            if span.root_deployment_id != root_deployment_id:
                continue
            shas.update(span.input_document_shas)
            shas.update(span.output_document_shas)
        return shas

    async def get_blob(self, content_sha256: str) -> _BlobRecord | None:
        return self._blobs.get(content_sha256)

    async def get_blobs_batch(self, content_sha256s: list[str]) -> dict[str, _BlobRecord]:
        return {sha256: self._blobs[sha256] for sha256 in content_sha256s if sha256 in self._blobs}

    async def get_span_logs(
        self,
        span_id: UUID,
        *,
        level: str | None = None,
        category: str | None = None,
    ) -> list[LogRecord]:
        return sorted(
            (log for log in self._logs if log.span_id == span_id and (level is None or log.level == level) and (category is None or log.category == category)),
            key=lambda log: (log.sequence_no, log.timestamp, str(log.span_id)),
        )

    async def get_deployment_logs(
        self,
        deployment_id: UUID,
        *,
        level: str | None = None,
        category: str | None = None,
    ) -> list[LogRecord]:
        return sorted(
            (
                log
                for log in self._logs
                if log.deployment_id == deployment_id and (level is None or log.level == level) and (category is None or log.category == category)
            ),
            key=log_sort_key,
        )

    async def get_deployment_logs_batch(
        self,
        deployment_ids: list[UUID],
        *,
        level: str | None = None,
        category: str | None = None,
    ) -> list[LogRecord]:
        allowed_ids = set(deployment_ids)
        return sorted(
            (
                log
                for log in self._logs
                if log.deployment_id in allowed_ids and (level is None or log.level == level) and (category is None or log.category == category)
            ),
            key=log_sort_key,
        )

    async def get_deployment_scoped_spans(self, root_deployment_id: UUID, deployment_id: UUID, *, include_meta: bool = True) -> list[SpanRecord]:
        matches = [s for s in self._spans.values() if s.root_deployment_id == root_deployment_id and s.deployment_id == deployment_id]
        return sorted(matches, key=span_sort_key)

    async def list_deployment_summaries(
        self,
        limit: int,
        *,
        status: str | None = None,
        root_only: bool = False,
        offset: int = 0,
    ) -> list[DeploymentSummaryRecord]:
        matches = [s for s in self._spans.values() if s.kind == SpanKind.DEPLOYMENT]
        if root_only:
            matches = [s for s in matches if s.span_id == s.root_deployment_id]
        if status is not None:
            matches = [s for s in matches if s.status == status]
        matches = sorted(matches, key=deployment_sort_key, reverse=True)[offset : offset + limit]
        results: list[DeploymentSummaryRecord] = []
        for span in matches:
            cost = sum(s.cost_usd for s in self._spans.values() if s.root_deployment_id == span.root_deployment_id)
            results.append(
                DeploymentSummaryRecord(
                    deployment_id=span.span_id,
                    root_deployment_id=span.root_deployment_id,
                    run_id=span.run_id,
                    deployment_name=span.deployment_name,
                    name=span.name,
                    status=span.status,
                    started_at=span.started_at,
                    ended_at=span.ended_at,
                    parent_span_id=span.parent_span_id,
                    cost_usd=cost,
                )
            )
        return results

    async def list_tree_deployments(self, root_deployment_id: UUID) -> list[DeploymentSummaryRecord]:
        dep_spans = [s for s in self._spans.values() if s.root_deployment_id == root_deployment_id and s.kind == SpanKind.DEPLOYMENT]
        cost_by_dep: dict[UUID, float] = {}
        for s in self._spans.values():
            if s.root_deployment_id == root_deployment_id:
                cost_by_dep[s.deployment_id] = cost_by_dep.get(s.deployment_id, 0.0) + s.cost_usd
        return sorted(
            [
                DeploymentSummaryRecord(
                    deployment_id=s.span_id,
                    root_deployment_id=s.root_deployment_id,
                    run_id=s.run_id,
                    deployment_name=s.deployment_name,
                    name=s.name,
                    status=s.status,
                    started_at=s.started_at,
                    ended_at=s.ended_at,
                    parent_span_id=s.parent_span_id,
                    cost_usd=cost_by_dep.get(s.span_id, 0.0),
                )
                for s in dep_spans
            ],
            key=lambda r: (r.started_at, str(r.deployment_id)),
        )

    async def get_document_producers(self, root_deployment_id: UUID) -> dict[str, DocumentProducerRecord]:
        producers: dict[str, DocumentProducerRecord] = {}
        for s in sorted(self._spans.values(), key=lambda s: s.started_at):
            if s.root_deployment_id != root_deployment_id or s.kind in {SpanKind.DEPLOYMENT, SpanKind.ATTEMPT}:
                continue
            for sha in s.output_document_shas:
                if sha not in producers:
                    producers[sha] = DocumentProducerRecord(
                        document_sha256=sha,
                        span_id=s.span_id,
                        span_name=s.name,
                        span_kind=s.kind,
                        deployment_id=s.deployment_id,
                        deployment_name=s.deployment_name,
                        started_at=s.started_at,
                    )
        return producers

    async def get_document_events(
        self,
        root_deployment_id: UUID,
        *,
        deployment_id: UUID | None = None,
        limit: int = 100,
        offset: int = 0,
        since: datetime | None = None,
        event_types: list[str] | None = None,
    ) -> DocumentEventPage:
        event_type_set = set(event_types) if event_types else None
        events: list[DocumentEventRecord] = []
        for s in self._spans.values():
            if s.root_deployment_id != root_deployment_id:
                continue
            if deployment_id is not None and s.deployment_id != deployment_id:
                continue
            if s.kind in {SpanKind.DEPLOYMENT, SpanKind.ATTEMPT}:
                continue
            for sha in s.output_document_shas:
                ts = s.ended_at or s.started_at
                et = f"{s.kind}_output"
                if (since is not None and ts <= since) or (event_type_set is not None and et not in event_type_set):
                    continue
                events.append(
                    DocumentEventRecord(
                        document_sha256=sha,
                        span_id=s.span_id,
                        span_name=s.name,
                        span_kind=s.kind,
                        deployment_id=s.deployment_id,
                        timestamp=ts,
                        direction="output",
                    )
                )
            for sha in s.input_document_shas:
                ts = s.started_at
                et = f"{s.kind}_input"
                if (since is not None and ts <= since) or (event_type_set is not None and et not in event_type_set):
                    continue
                events.append(
                    DocumentEventRecord(
                        document_sha256=sha,
                        span_id=s.span_id,
                        span_name=s.name,
                        span_kind=s.kind,
                        deployment_id=s.deployment_id,
                        timestamp=ts,
                        direction="input",
                    )
                )
        total = len(events)
        events.sort(key=lambda e: e.timestamp, reverse=True)
        return DocumentEventPage(events=tuple(events[offset : offset + limit]), total_events=total)

    async def find_documents_by_name(
        self,
        names: list[str],
        *,
        document_type: str | None = None,
    ) -> dict[str, DocumentRecord]:
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
