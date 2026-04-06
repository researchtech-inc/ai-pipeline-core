# MODULE: database
# CLASSES: DatabaseReader, SpanKind, SpanStatus, SpanRecord, DocumentRecord, CostTotals, HydratedDocument
# DEPENDS: Protocol, StrEnum
# PURPOSE: Unified database module for the span-based schema.
# VERSION: 0.21.0
# AUTO-GENERATED from source code — do not edit. Run: make docs-ai-build

## Imports

```python
from ai_pipeline_core import DatabaseReader
from ai_pipeline_core.database import CostTotals, Database, DocumentRecord, HydratedDocument, SpanKind, SpanRecord, SpanStatus
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
    """Read protocol for the span/document/blob/log schema."""

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

    async def get_all_document_shas_for_tree(self, root_deployment_id: UUID) -> set[str]:
        """Collect all document SHA256s referenced anywhere in a deployment tree."""
        ...

    async def get_blob(self, content_sha256: str) -> _BlobRecord | None:
        """Retrieve a blob by content SHA256."""
        ...

    async def get_blobs_batch(self, content_sha256s: list[str]) -> dict[str, _BlobRecord]:
        """Retrieve blobs keyed by content SHA256."""
        ...

    async def get_cached_completion(
        self,
        cache_key: str,
        *,
        max_age: timedelta | None = None,
    ) -> SpanRecord | None:
        """Find a completed span matching the cache key within the max age window."""
        ...

    async def get_child_spans(self, parent_span_id: UUID) -> list[SpanRecord]:
        """Retrieve direct child spans ordered by sequence number."""
        ...

    async def get_deployment_by_run_id(self, run_id: str) -> SpanRecord | None:
        """Find the newest deployment span for a run ID."""
        ...

    async def get_deployment_cost_totals(self, root_deployment_id: UUID) -> CostTotals:
        """Aggregate cost (all spans) and token totals (llm_round only) for a deployment tree."""
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

    async def get_deployment_span_count(
        self,
        root_deployment_id: UUID,
        *,
        kinds: list[str] | None = None,
    ) -> int:
        """Count spans in a deployment tree, optionally filtering by span kind."""
        ...

    async def get_deployment_tree(self, root_deployment_id: UUID) -> list[SpanRecord]:
        """Retrieve every span in a deployment tree as a flat list."""
        ...

    async def get_document(self, document_sha256: str) -> DocumentRecord | None:
        """Retrieve a document record by SHA256."""
        ...

    async def get_document_with_content(
        self,
        document_sha256: str,
    ) -> HydratedDocument | None:
        """Load document metadata plus primary content and attachment blobs."""
        ...

    async def get_documents_batch(self, sha256s: list[str]) -> dict[str, DocumentRecord]:
        """Retrieve multiple document records keyed by SHA256."""
        ...

    async def get_span(self, span_id: UUID) -> SpanRecord | None:
        """Retrieve a span by its ID."""
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

    async def get_spans_referencing_document(
        self,
        document_sha256: str,
        *,
        kinds: list[str] | None = None,
    ) -> list[SpanRecord]:
        """Find spans that reference a SHA in document or blob input/output arrays."""
        ...

    async def list_deployments(
        self,
        limit: int,
        *,
        status: str | None = None,
    ) -> list[SpanRecord]:
        """List deployment spans ordered by newest start time first."""
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
```

## Examples

**Database reader is runtime checkable** (`tests/database/test_protocol.py:93`)

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

**Database writer method signatures** (`tests/database/test_protocol.py:168`)

```python
def test_database_writer_method_signatures() -> None:
    _assert_signature(DatabaseWriter, "insert_span", parameter_types={"span": SpanRecord}, return_type=type(None))
    _assert_signature(DatabaseWriter, "save_document", parameter_types={"record": DocumentRecord}, return_type=type(None))
    _assert_signature(DatabaseWriter, "save_blob", parameter_types={"blob": _BlobRecord}, return_type=type(None))
    _assert_signature(DatabaseWriter, "save_logs_batch", parameter_types={"logs": list[LogRecord]}, return_type=type(None))
```

**Memory database conforms to protocols** (`tests/database/test_protocol.py:86`)

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

**Span status members** (`tests/database/test_types.py:34`)

```python
def test_span_status_members() -> None:
    assert tuple(status.value for status in SpanStatus) == (
        "running",
        "completed",
        "failed",
        "cached",
        "skipped",
    )
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
