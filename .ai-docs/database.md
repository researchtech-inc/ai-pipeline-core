# MODULE: database
# CLASSES: DatabaseReader, SpanKind, SpanStatus, SpanRecord, DocumentRecord, CostTotals, HydratedDocument, DeploymentSummaryRecord, DocumentProducerRecord, DocumentEventRecord, DocumentEventPage
# DEPENDS: Protocol, StrEnum
# PURPOSE: Unified database module for the span-based schema.
# VERSION: 0.22.3
# AUTO-GENERATED from source code — do not edit. Run: make docs-ai-build

## Imports

```python
from ai_pipeline_core.database import (
    CostTotals,
    Database,
    DatabaseReader,
    DeploymentSummaryRecord,
    DocumentEventPage,
    DocumentEventRecord,
    DocumentProducerRecord,
    DocumentRecord,
    HydratedDocument,
    SpanKind,
    SpanRecord,
    SpanStatus,
    aggregate_cost_totals,
)
```

## Types & Constants

```python
Database = _MemoryDatabase | FilesystemDatabase | ClickHouseDatabase
```

## Internal Types

```python
@dataclass(frozen=True, slots=True)
class _BlobRecord:
    """Row from the immutable blobs table."""

    content_sha256: str
    content: bytes
```

## Public API

```python
# Protocol — implement in concrete class
@runtime_checkable
class DatabaseReader(Protocol):
    """Read protocol for span storage."""

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

    async def get_all_document_shas_for_tree(self, root_deployment_id: UUID) -> set[str]:
        """Collect all document SHAs in a tree."""
        ...

    async def get_blob(self, content_sha256: str) -> _BlobRecord | None:
        """Load one blob."""
        ...

    async def get_blobs_batch(self, content_sha256s: list[str]) -> dict[str, _BlobRecord]:
        """Load many blobs keyed by SHA."""
        ...

    async def get_cached_completion(
        self,
        cache_key: str,
        *,
        max_age: timedelta | None = None,
    ) -> SpanRecord | None:
        """Find a completed cached span."""
        ...

    async def get_child_spans(self, parent_span_id: UUID) -> list[SpanRecord]:
        """Load direct child spans."""
        ...

    async def get_deployment_by_run_id(self, run_id: str) -> SpanRecord | None:
        """Find the newest deployment span for a run."""
        ...

    async def get_deployment_cost_totals(self, root_deployment_id: UUID) -> CostTotals:
        """Aggregate deployment cost totals."""
        ...

    async def get_deployment_latest_activity(self, root_deployment_id: UUID) -> datetime | None:
        """Return latest tree activity."""
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

    async def get_deployment_span_count(
        self,
        root_deployment_id: UUID,
        *,
        kinds: list[str] | None = None,
    ) -> int:
        """Count spans in a tree."""
        ...

    async def get_deployment_tree(self, root_deployment_id: UUID) -> list[SpanRecord]:
        """Load one deployment tree."""
        ...

    async def get_deployment_tree_topology(self, root_deployment_id: UUID) -> list[SpanRecord]:
        """Load one deployment tree without payload JSON."""
        ...

    async def get_document(self, document_sha256: str) -> DocumentRecord | None:
        """Load one document record."""
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

    async def get_document_producers(
        self,
        root_deployment_id: UUID,
    ) -> dict[str, DocumentProducerRecord]:
        """Return the earliest producer span for each document in a root tree."""
        ...

    async def get_document_with_content(
        self,
        document_sha256: str,
    ) -> HydratedDocument | None:
        """Load one document plus content."""
        ...

    async def get_documents_batch(self, sha256s: list[str]) -> dict[str, DocumentRecord]:
        """Load many document records keyed by SHA."""
        ...

    async def get_span(self, span_id: UUID) -> SpanRecord | None:
        """Load one span by ID."""
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

    async def get_spans_referencing_document(
        self,
        document_sha256: str,
        *,
        kinds: list[str] | None = None,
    ) -> list[SpanRecord]:
        """Find spans that reference a document or blob SHA."""
        ...

    async def latest_span_activity_for_deployment(
        self,
        root_deployment_id: UUID,
    ) -> datetime | None:
        """Return latest tree activity for recovery."""
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

    async def list_tree_deployments(
        self,
        root_deployment_id: UUID,
    ) -> list[DeploymentSummaryRecord]:
        """List deployment spans within one root tree, with aggregated cost_usd per deployment."""
        ...


# Enum
class SpanKind(StrEnum):
    """Discriminator for span-based execution records."""

    DEPLOYMENT = "deployment"
    FLOW = "flow"
    TASK = "task"
    ATTEMPT = "attempt"
    OPERATION = "operation"
    CONVERSATION = "conversation"
    LLM_ROUND = "llm_round"
    TOOL_CALL = "tool_call"


# Enum
class SpanStatus(StrEnum):
    """Lifecycle status for span-based execution records."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CACHED = "cached"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class SpanRecord:
    """Row from the span-oriented execution table."""

    span_id: UUID
    parent_span_id: UUID | None
    deployment_id: UUID
    root_deployment_id: UUID
    run_id: str
    kind: str
    name: str
    sequence_no: int
    deployment_name: str = ""
    description: str = ""
    status: str = SpanStatus.RUNNING
    started_at: datetime = field(default_factory=_utcnow)
    ended_at: datetime | None = None
    version: int = 1
    cache_key: str = ""
    previous_conversation_id: UUID | None = None
    cost_usd: float = 0.0
    error_type: str = ""
    error_message: str = ""
    input_document_shas: tuple[str, ...] = ()
    output_document_shas: tuple[str, ...] = ()
    target: str = ""
    receiver_json: str = ""
    input_json: str = ""
    output_json: str = ""
    error_json: str = ""
    meta_json: str = ""
    metrics_json: str = ""
    input_blob_shas: tuple[str, ...] = ()
    output_blob_shas: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_enum_string("kind", self.kind, SpanKind)
        _validate_enum_string("status", self.status, SpanStatus)
        _validate_string_tuple("input_document_shas", self.input_document_shas)
        _validate_string_tuple("output_document_shas", self.output_document_shas)
        _validate_string_tuple("input_blob_shas", self.input_blob_shas)
        _validate_string_tuple("output_blob_shas", self.output_blob_shas)


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    """Row from the content-addressed documents table."""

    document_sha256: DocumentSha256
    content_sha256: str
    document_type: str
    name: str
    description: str = ""
    mime_type: str = ""
    size_bytes: int = 0
    summary: str = ""
    derived_from: tuple[str, ...] = ()
    triggered_by: tuple[str, ...] = ()
    attachment_names: tuple[str, ...] = ()
    attachment_descriptions: tuple[str, ...] = ()
    attachment_content_sha256s: tuple[str, ...] = ()
    attachment_mime_types: tuple[str, ...] = ()
    attachment_size_bytes: tuple[int, ...] = ()
    publicly_visible: bool = False

    def __post_init__(self) -> None:
        _validate_string_tuple("derived_from", self.derived_from)
        _validate_string_tuple("triggered_by", self.triggered_by)
        _validate_string_tuple("attachment_names", self.attachment_names)
        _validate_string_tuple("attachment_descriptions", self.attachment_descriptions)
        _validate_string_tuple("attachment_content_sha256s", self.attachment_content_sha256s)
        _validate_string_tuple("attachment_mime_types", self.attachment_mime_types)
        _validate_int_tuple("attachment_size_bytes", self.attachment_size_bytes)
        attachment_count = len(self.attachment_names)
        attachment_lengths = (
            len(self.attachment_descriptions),
            len(self.attachment_content_sha256s),
            len(self.attachment_mime_types),
            len(self.attachment_size_bytes),
        )
        if any(length != attachment_count for length in attachment_lengths):
            msg = (
                "DocumentRecord attachment fields must have matching lengths. "
                "Provide one name, description, content_sha256, mime_type, and size_bytes entry for each attachment."
            )
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class CostTotals:
    """Aggregated cost and token totals for a deployment. Cost includes all span kinds, token counts include llm_round spans only."""

    cost_usd: float = 0.0
    tokens_input: int = 0
    tokens_output: int = 0
    tokens_cache_read: int = 0
    tokens_reasoning: int = 0


@dataclass(frozen=True, slots=True)
class HydratedDocument:
    """Document metadata with loaded primary and attachment blob content."""

    record: DocumentRecord
    content: bytes
    attachment_contents: dict[str, bytes] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_bytes_mapping("attachment_contents", self.attachment_contents)


@dataclass(frozen=True, slots=True)
class DeploymentSummaryRecord:
    """Lightweight deployment record for list/summary views."""

    deployment_id: UUID
    root_deployment_id: UUID
    run_id: str
    deployment_name: str
    name: str
    status: str
    started_at: datetime
    ended_at: datetime | None
    parent_span_id: UUID | None
    cost_usd: float


@dataclass(frozen=True, slots=True)
class DocumentProducerRecord:
    """Maps a document to the span that first produced it."""

    document_sha256: str
    span_id: UUID
    span_name: str
    span_kind: str
    deployment_id: UUID
    deployment_name: str
    started_at: datetime


@dataclass(frozen=True, slots=True)
class DocumentEventRecord:
    """Lightweight record for document input/output events."""

    document_sha256: str
    span_id: UUID
    span_name: str
    span_kind: str
    deployment_id: UUID
    timestamp: datetime
    direction: str  # "input" | "output"


@dataclass(frozen=True, slots=True)
class DocumentEventPage:
    """Filtered document events plus total count."""

    events: tuple[DocumentEventRecord, ...]
    total_events: int
```

## Functions

```python
def create_debug_sink(
    output_dir: Path,
    *,
    parent: FilesystemDatabase | None = None,
) -> FilesystemDatabase:
    """Create a writable FilesystemDatabase at output_dir for replay/debug output.

    When parent is provided, writes overlay_meta.json recording the parent
    snapshot path so the relationship can be reopened later by tooling.

    Raises FileExistsError if the directory already contains database artifact directories.
    """
    if output_dir.exists():
        existing = {d.name for d in output_dir.iterdir() if d.is_dir()} & _DB_ARTIFACT_DIRS
        if existing:
            raise FileExistsError(
                f"Output directory {output_dir} already contains database artifacts ({', '.join(sorted(existing))}). "
                "Use a fresh directory for replay/debug output, or remove the existing artifacts first."
            )

    db = FilesystemDatabase(output_dir)

    if parent is not None:
        meta = {
            "parent_path": str(parent.base_path.resolve()),
            "created_at": datetime.now(UTC).isoformat(),
            "type": "overlay",
        }
        meta_path = output_dir / "overlay_meta.json"
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    return db


def aggregate_cost_totals(items: Iterable[tuple[str, float, str, str]]) -> CostTotals:
    cost_usd = 0.0
    tokens_input = 0
    tokens_output = 0
    tokens_cache_read = 0
    tokens_reasoning = 0
    for kind, span_cost_usd, metrics_json, context in items:
        cost_usd += float(span_cost_usd)
        if kind != SpanKind.LLM_ROUND:
            continue
        metrics = parse_json_object(metrics_json, context=context, field_name="metrics_json")
        tokens_input += get_token_count(metrics, TOKENS_INPUT_KEY)
        tokens_output += get_token_count(metrics, TOKENS_OUTPUT_KEY)
        tokens_cache_read += get_token_count(metrics, TOKENS_CACHE_READ_KEY)
        tokens_reasoning += get_token_count(metrics, TOKENS_REASONING_KEY)
    return CostTotals(
        cost_usd=cost_usd,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        tokens_cache_read=tokens_cache_read,
        tokens_reasoning=tokens_reasoning,
    )
```

## Examples

**Database reader is runtime checkable** (`tests/database/test_protocol.py:103`)

```python
def test_database_reader_is_runtime_checkable() -> None:
    assert getattr(DatabaseReader, "_is_runtime_protocol", False) is True
    assert isinstance(_make_reader_stub(), DatabaseReader)
    assert not isinstance(object(), DatabaseReader)
```

**Allows empty existing dir** (`tests/database/test_filesystem_overlay.py:45`)

```python
def test_allows_empty_existing_dir(self, tmp_path) -> None:
    output = tmp_path / "exists"
    output.mkdir()
    (output / "notes.txt").write_text("hello")
    db = create_debug_sink(output)
    assert isinstance(db, FilesystemDatabase)
```

**Allows nonexistent dir** (`tests/database/test_filesystem_overlay.py:52`)

```python
def test_allows_nonexistent_dir(self, tmp_path) -> None:
    db = create_debug_sink(tmp_path / "new" / "deep" / "out")
    assert isinstance(db, FilesystemDatabase)
```

**Creates database** (`tests/database/test_filesystem_overlay.py:12`)

```python
def test_creates_database(self, tmp_path) -> None:
    db = create_debug_sink(tmp_path / "out")
    assert isinstance(db, FilesystemDatabase)
```

**Database writer method signatures** (`tests/database/test_protocol.py:209`)

```python
def test_database_writer_method_signatures() -> None:
    _assert_signature(DatabaseWriter, "insert_span", parameter_types={"span": SpanRecord}, return_type=type(None))
    _assert_signature(DatabaseWriter, "save_document", parameter_types={"record": DocumentRecord}, return_type=type(None))
    _assert_signature(DatabaseWriter, "save_blob", parameter_types={"blob": _BlobRecord}, return_type=type(None))
    _assert_signature(DatabaseWriter, "save_logs_batch", parameter_types={"logs": list[LogRecord]}, return_type=type(None))
```

**Empty tree** (`tests/database/test_new_reader_methods.py:844`)

```python
async def test_empty_tree(self) -> None:
    db = _MemoryDatabase()
    page = await db.get_document_events(uuid4())
    assert page == DocumentEventPage(events=(), total_events=0)
```

**Memory database conforms to protocols** (`tests/database/test_protocol.py:96`)

```python
def test_memory_database_conforms_to_protocols() -> None:
    database = _MemoryDatabase()
    assert isinstance(database, DatabaseReader)
    assert isinstance(database, DatabaseWriter)
    assert database.supports_remote is False
```

**No parent no meta** (`tests/database/test_filesystem_overlay.py:29`)

```python
def test_no_parent_no_meta(self, tmp_path) -> None:
    create_debug_sink(tmp_path / "out")
    assert not (tmp_path / "out" / "overlay_meta.json").exists()
```


## Error Examples

**Rejects existing documents dir** (`tests/database/test_filesystem_overlay.py:39`)

```python
def test_rejects_existing_documents_dir(self, tmp_path) -> None:
    output = tmp_path / "exists"
    (output / "documents").mkdir(parents=True)
    with pytest.raises(FileExistsError, match="already contains database artifacts"):
        create_debug_sink(output)
```

**Rejects existing spans dir** (`tests/database/test_filesystem_overlay.py:33`)

```python
def test_rejects_existing_spans_dir(self, tmp_path) -> None:
    output = tmp_path / "exists"
    (output / "spans").mkdir(parents=True)
    with pytest.raises(FileExistsError, match="already contains database artifacts"):
        create_debug_sink(output)
```

**Tampered content raises** (`tests/database/test_bugs_sha256_roundtrip.py:245`)

```python
def test_tampered_content_raises(self) -> None:
    doc = RoundTripDoc(name="test.txt", content=b"original")
    record = document_to_record(doc)
    hydrated = HydratedDocument(record=record, content=b"TAMPERED", attachment_contents={})
    with pytest.raises(ValueError, match="integrity check failed"):
        hydrate_document(RoundTripDoc, hydrated)
```

**Wrong primary content raises** (`tests/database/test_bugs_sha256_roundtrip.py:215`)

```python
def test_wrong_primary_content_raises(self) -> None:
    doc = RoundTripDoc(name="image.png", content=MINIMAL_PNG)
    record = document_to_record(doc)
    hydrated = HydratedDocument(record=record, content=b"wrong content", attachment_contents={})
    with pytest.raises(ValueError, match="integrity check failed"):
        hydrate_document(RoundTripDoc, hydrated)
```

**Attachment contents raises on missing blobs** (`tests/database/test_bugs_documents.py:65`)

```python
def test_attachment_contents_raises_on_missing_blobs() -> None:
    """_attachment_contents_for_record raises ValueError when blobs are missing."""
    record = DocumentRecord(
        document_sha256="doc_sha",
        content_sha256="content_sha",
        document_type="SampleDoc",
        name="test",
        attachment_names=("att1.txt", "att2.txt"),
        attachment_descriptions=("", ""),
        attachment_content_sha256s=("att_sha1", "att_sha2"),
        attachment_mime_types=("text/plain", "text/plain"),
        attachment_size_bytes=(10, 20),
    )
    blobs = {"att_sha1": _BlobRecord(content_sha256="att_sha1", content=b"data1")}
    blobs = {"att_sha1": _BlobRecord(content_sha256="att_sha1", content=b"data1")}

    with pytest.raises(ValueError, match=r"missing attachment blob"):
        _attachment_contents_for_record(record, blobs)
```

**Blob record defaults and immutability** (`tests/database/test_types.py:125`)

```python
def test_blob_record_defaults_and_immutability() -> None:
    blob = _BlobRecord(content_sha256="blob-sha", content=b"hello")
    assert blob.content_sha256 == "blob-sha"
    assert blob.content == b"hello"

    with pytest.raises(dataclasses.FrozenInstanceError):
        blob.content = b"changed"  # type: ignore[misc]
```

**Document record rejects mismatched attachment lengths** (`tests/database/test_types.py:110`)

```python
def test_document_record_rejects_mismatched_attachment_lengths() -> None:
    with pytest.raises(ValueError, match="matching lengths"):
        DocumentRecord(
            document_sha256="doc-sha",
            content_sha256="blob-sha",
            document_type="ExampleDocument",
            name="example.md",
            attachment_names=("a.txt",),
            attachment_descriptions=(),
            attachment_content_sha256s=("blob-a",),
            attachment_mime_types=("text/plain",),
            attachment_size_bytes=(1,),
        )
```

**Ensure schema raises on newer db** (`tests/database/test_clickhouse.py:201`)

```python
@pytest.mark.asyncio
async def test_ensure_schema_raises_on_newer_db() -> None:
    client = _mock_client(table_exists=True, db_version=SCHEMA_VERSION + 1)

    with pytest.raises(SchemaVersionError, match="newer than the framework supports"):
        await _ensure_schema(client, "default")
```
