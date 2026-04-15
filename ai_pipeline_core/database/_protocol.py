"""Database read/write protocols for the span-based schema."""

from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable
from uuid import UUID

from ai_pipeline_core.database._types import (
    CostTotals,
    DeploymentSummaryRecord,
    DocumentEventPage,
    DocumentProducerRecord,
    DocumentRecord,
    HydratedDocument,
    LogRecord,
    SpanRecord,
    _BlobRecord,
)

__all__ = [
    "DatabaseReader",
    "DatabaseWriter",
]


# Protocol
@runtime_checkable
class DatabaseWriter(Protocol):
    """Write protocol for span storage."""

    @property
    def supports_remote(self) -> bool:
        """Whether this backend supports Prefect-based remote deployment execution."""
        ...

    async def insert_span(self, span: SpanRecord) -> None:
        """Append one span version."""
        ...

    async def save_document(self, record: DocumentRecord) -> None:
        """Store one document record."""
        ...

    async def save_document_batch(self, records: list[DocumentRecord]) -> None:
        """Store many document records."""
        ...

    async def save_blob(self, blob: _BlobRecord) -> None:
        """Store one blob."""
        ...

    async def save_blob_batch(self, blobs: list[_BlobRecord]) -> None:
        """Store many blobs."""
        ...

    async def save_logs_batch(self, logs: list[LogRecord]) -> None:
        """Store many logs."""
        ...

    async def update_document_summary(self, document_sha256: str, summary: str) -> None:
        """Update one document summary."""
        ...

    async def flush(self) -> None:
        """Flush buffered writes."""
        ...

    async def shutdown(self) -> None:
        """Close the backend."""
        ...


# Protocol
@runtime_checkable
class DatabaseReader(Protocol):
    """Read protocol for span storage."""

    async def get_span(self, span_id: UUID) -> SpanRecord | None:
        """Load one span by ID."""
        ...

    async def get_child_spans(self, parent_span_id: UUID) -> list[SpanRecord]:
        """Load direct child spans."""
        ...

    async def get_deployment_tree(self, root_deployment_id: UUID) -> list[SpanRecord]:
        """Load one deployment tree."""
        ...

    async def get_deployment_tree_topology(self, root_deployment_id: UUID) -> list[SpanRecord]:
        """Load one deployment tree without payload JSON."""
        ...

    async def get_deployment_latest_activity(self, root_deployment_id: UUID) -> datetime | None:
        """Return latest tree activity."""
        ...

    async def get_deployment_by_run_id(self, run_id: str) -> SpanRecord | None:
        """Find the newest deployment span for a run."""
        ...

    async def list_deployments(
        self,
        limit: int,
        *,
        status: str | None = None,
        root_only: bool = False,
        offset: int = 0,
    ) -> list[SpanRecord]:
        """List deployment spans, ordered by started_at DESC."""
        ...

    async def list_deployments_by_run_id(self, run_id: str) -> list[SpanRecord]:
        """List deployment spans for one run."""
        ...

    async def list_running_deployment_roots(
        self,
        *,
        limit: int = 1000,
    ) -> list[SpanRecord]:
        """List running root deployments, oldest first."""
        ...

    async def latest_span_activity_for_deployment(
        self,
        root_deployment_id: UUID,
    ) -> datetime | None:
        """Return latest tree activity for recovery."""
        ...

    async def get_cached_completion(
        self,
        cache_key: str,
        *,
        max_age: timedelta | None = None,
    ) -> SpanRecord | None:
        """Find a completed cached span."""
        ...

    async def get_deployment_cost_totals(self, root_deployment_id: UUID) -> CostTotals:
        """Aggregate deployment cost totals."""
        ...

    async def get_deployment_span_count(
        self,
        root_deployment_id: UUID,
        *,
        kinds: list[str] | None = None,
    ) -> int:
        """Count spans in a tree."""
        ...

    async def get_spans_referencing_document(
        self,
        document_sha256: str,
        *,
        kinds: list[str] | None = None,
    ) -> list[SpanRecord]:
        """Find spans that reference a document or blob SHA."""
        ...

    async def get_document(self, document_sha256: str) -> DocumentRecord | None:
        """Load one document record."""
        ...

    async def get_documents_batch(self, sha256s: list[str]) -> dict[str, DocumentRecord]:
        """Load many document records keyed by SHA."""
        ...

    async def get_document_with_content(
        self,
        document_sha256: str,
    ) -> HydratedDocument | None:
        """Load one document plus content."""
        ...

    async def get_all_document_shas_for_tree(self, root_deployment_id: UUID) -> set[str]:
        """Collect all document SHAs in a tree."""
        ...

    async def get_blob(self, content_sha256: str) -> _BlobRecord | None:
        """Load one blob."""
        ...

    async def get_blobs_batch(self, content_sha256s: list[str]) -> dict[str, _BlobRecord]:
        """Load many blobs keyed by SHA."""
        ...

    async def get_span_logs(
        self,
        span_id: UUID,
        *,
        level: str | None = None,
        category: str | None = None,
    ) -> list[LogRecord]:
        """Load logs for one span."""
        ...

    async def get_deployment_logs(
        self,
        deployment_id: UUID,
        *,
        level: str | None = None,
        category: str | None = None,
    ) -> list[LogRecord]:
        """Load logs for one deployment."""
        ...

    async def get_deployment_logs_batch(
        self,
        deployment_ids: list[UUID],
        *,
        level: str | None = None,
        category: str | None = None,
    ) -> list[LogRecord]:
        """Load logs for many deployments."""
        ...

    async def get_deployment_scoped_spans(
        self,
        root_deployment_id: UUID,
        deployment_id: UUID,
        *,
        include_meta: bool = True,
    ) -> list[SpanRecord]:
        """Load latest spans for exactly one deployment within a root tree.

        When *include_meta* is True (default), ``meta_json`` and ``metrics_json``
        are populated; heavy payload columns are always blanked. When False, all
        JSON fields are blanked (topology mode). Use ``get_span()`` for full payloads.
        """
        ...

    async def list_deployment_summaries(
        self,
        limit: int,
        *,
        status: str | None = None,
        root_only: bool = False,
        offset: int = 0,
    ) -> list[DeploymentSummaryRecord]:
        """List deployment spans with minimal columns and aggregated cost_usd."""
        ...

    async def list_tree_deployments(
        self,
        root_deployment_id: UUID,
    ) -> list[DeploymentSummaryRecord]:
        """List deployment spans within one root tree, with aggregated cost_usd per deployment."""
        ...

    async def get_document_producers(
        self,
        root_deployment_id: UUID,
    ) -> dict[str, DocumentProducerRecord]:
        """Return the earliest producer span for each document in a root tree."""
        ...

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
        """Return filtered document events plus total count, ordered by recency.

        ``total_events`` reflects the count after since/event_types filtering,
        before limit/offset.
        """
        ...

    async def find_documents_by_name(
        self,
        names: list[str],
        *,
        document_type: str | None = None,
    ) -> dict[str, DocumentRecord]:
        """Find documents by exact name.

        Returns ``{name: record}``. Duplicate names keep the highest SHA.
        """
        ...
