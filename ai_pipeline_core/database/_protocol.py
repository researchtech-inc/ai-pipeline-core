"""Database read/write protocols for the span-based schema."""

from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable
from uuid import UUID

from ai_pipeline_core.database._types import (
    CostTotals,
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
    """Write protocol for the span/document/blob/log schema."""

    @property
    def supports_remote(self) -> bool:
        """Whether this backend supports Prefect-based remote deployment execution."""
        ...

    async def insert_span(self, span: SpanRecord) -> None:
        """Insert a span row. Lifecycle updates are written as new rows with higher versions."""
        ...

    async def save_document(self, record: DocumentRecord) -> None:
        """Persist a single document record."""
        ...

    async def save_document_batch(self, records: list[DocumentRecord]) -> None:
        """Persist multiple document records in one operation."""
        ...

    async def save_blob(self, blob: _BlobRecord) -> None:
        """Persist a single blob."""
        ...

    async def save_blob_batch(self, blobs: list[_BlobRecord]) -> None:
        """Persist multiple blobs in one operation."""
        ...

    async def save_logs_batch(self, logs: list[LogRecord]) -> None:
        """Persist multiple log records in one operation."""
        ...

    async def update_document_summary(self, document_sha256: str, summary: str) -> None:
        """Update the top-level summary field for a document row."""
        ...

    async def flush(self) -> None:
        """Flush any buffered writes to storage."""
        ...

    async def shutdown(self) -> None:
        """Release resources and close connections."""
        ...


# Protocol
@runtime_checkable
class DatabaseReader(Protocol):
    """Read protocol for the span/document/blob/log schema."""

    async def get_span(self, span_id: UUID) -> SpanRecord | None:
        """Retrieve a span by its ID."""
        ...

    async def get_child_spans(self, parent_span_id: UUID) -> list[SpanRecord]:
        """Retrieve direct child spans ordered by sequence number."""
        ...

    async def get_deployment_tree(self, root_deployment_id: UUID) -> list[SpanRecord]:
        """Retrieve every span in a deployment tree as a flat list."""
        ...

    async def get_deployment_by_run_id(self, run_id: str) -> SpanRecord | None:
        """Find the newest deployment span for a run ID."""
        ...

    async def list_deployments(
        self,
        limit: int,
        *,
        status: str | None = None,
        root_only: bool = False,
    ) -> list[SpanRecord]:
        """List deployment spans ordered by newest start time first."""
        ...

    async def list_deployments_by_run_id(self, run_id: str) -> list[SpanRecord]:
        """List deployment spans for an exact run_id ordered by newest start time first."""
        ...

    async def list_orphaned_deployment_roots(
        self,
        *,
        older_than: datetime,
        limit: int = 1000,
    ) -> list[SpanRecord]:
        """List root deployment spans still marked running after the given cutoff."""
        ...

    async def get_cached_completion(
        self,
        cache_key: str,
        *,
        max_age: timedelta | None = None,
    ) -> SpanRecord | None:
        """Find a completed span matching the cache key within the max age window."""
        ...

    async def get_deployment_cost_totals(self, root_deployment_id: UUID) -> CostTotals:
        """Aggregate cost (all spans) and token totals (llm_round only) for a deployment tree."""
        ...

    async def get_deployment_span_count(
        self,
        root_deployment_id: UUID,
        *,
        kinds: list[str] | None = None,
    ) -> int:
        """Count spans in a deployment tree, optionally filtering by span kind."""
        ...

    async def get_spans_referencing_document(
        self,
        document_sha256: str,
        *,
        kinds: list[str] | None = None,
    ) -> list[SpanRecord]:
        """Find spans that reference a SHA in document or blob input/output arrays."""
        ...

    async def get_document(self, document_sha256: str) -> DocumentRecord | None:
        """Retrieve a document record by SHA256."""
        ...

    async def get_documents_batch(self, sha256s: list[str]) -> dict[str, DocumentRecord]:
        """Retrieve multiple document records keyed by SHA256."""
        ...

    async def get_document_with_content(
        self,
        document_sha256: str,
    ) -> HydratedDocument | None:
        """Load document metadata plus primary content and attachment blobs."""
        ...

    async def get_all_document_shas_for_tree(self, root_deployment_id: UUID) -> set[str]:
        """Collect all document SHA256s referenced anywhere in a deployment tree."""
        ...

    async def get_blob(self, content_sha256: str) -> _BlobRecord | None:
        """Retrieve a blob by content SHA256."""
        ...

    async def get_blobs_batch(self, content_sha256s: list[str]) -> dict[str, _BlobRecord]:
        """Retrieve blobs keyed by content SHA256."""
        ...

    async def get_span_logs(
        self,
        span_id: UUID,
        *,
        level: str | None = None,
        category: str | None = None,
    ) -> list[LogRecord]:
        """Retrieve logs for a specific span."""
        ...

    async def get_deployment_logs(
        self,
        deployment_id: UUID,
        *,
        level: str | None = None,
        category: str | None = None,
    ) -> list[LogRecord]:
        """Retrieve logs for an entire deployment."""
        ...

    async def get_deployment_logs_batch(
        self,
        deployment_ids: list[UUID],
        *,
        level: str | None = None,
        category: str | None = None,
    ) -> list[LogRecord]:
        """Retrieve logs for multiple deployments in one operation."""
        ...

    async def find_documents_by_name(
        self,
        names: list[str],
        *,
        document_type: str | None = None,
    ) -> dict[str, DocumentRecord]:
        """Find document records by exact name match.

        Returns {name: record}. When multiple documents share a name,
        the record with the highest document_sha256 wins (deterministic tiebreak).
        """
        ...
