"""Regression tests for document SHA256 round-trip integrity.

Covers bugs where a Document's sha256 or metadata changes after
save → load through each backend (Memory, Filesystem, ClickHouse).

Bugs proven:
- row_to_blob corrupts non-UTF-8 binary content from ClickHouse (hex-encoding)
- description "" ↔ None asymmetry in document_to_record / hydrate_document
- attachment description "" ↔ None asymmetry
- hydrate_document has no integrity verification against stored sha256
"""

import base64
from pathlib import Path

import pytest

from ai_pipeline_core.database._documents import document_to_blobs, document_to_record, load_documents_from_database
from ai_pipeline_core.database._hydrate import hydrate_document
from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.database._types import HydratedDocument, _BlobRecord
from ai_pipeline_core.database.clickhouse._rows import row_to_blob
from ai_pipeline_core.database.filesystem._backend import FilesystemDatabase
from ai_pipeline_core.documents._context import DocumentSha256
from ai_pipeline_core.documents._hashing import compute_content_sha256
from ai_pipeline_core.documents.attachment import Attachment
from ai_pipeline_core.documents.document import Document
from ai_pipeline_core.settings import Settings


# Minimal valid PNG (1x1 transparent pixel)
MINIMAL_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

# Minimal valid WebP (RIFF header + random binary)
WEBP_LIKE = b"RIFF\x76\xbc\x00\x00WEBPVP8 " + bytes(range(128, 256))

# Arbitrary binary with every byte value
ALL_BYTES = bytes(range(256))


class RoundTripDoc(Document):
    """Document subclass for round-trip tests."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hydrate_from_record(doc: Document) -> Document:
    """Simulate save → load without a database: Document → record → HydratedDocument → hydrate."""
    record = document_to_record(doc)
    blobs = document_to_blobs(doc)
    blob_map = {b.content_sha256: b.content for b in blobs}
    att_contents = {sha: blob_map[sha] for sha in record.attachment_content_sha256s}
    hydrated = HydratedDocument(record=record, content=blob_map[record.content_sha256], attachment_contents=att_contents)
    return hydrate_document(type(doc), hydrated)


async def _roundtrip_memory(doc: Document) -> Document:
    """Full round-trip through MemoryDatabase."""
    db = _MemoryDatabase()
    record = document_to_record(doc)
    blobs = document_to_blobs(doc)
    await db.save_blob_batch(blobs)
    await db.save_document(record)
    loaded = await load_documents_from_database(db, {doc.sha256})
    assert len(loaded) == 1
    return loaded[0]


async def _roundtrip_filesystem(doc: Document, tmp_path: Path) -> Document:
    """Full round-trip through FilesystemDatabase."""
    db = FilesystemDatabase(tmp_path)
    record = document_to_record(doc)
    blobs = document_to_blobs(doc)
    await db.save_blob_batch(blobs)
    await db.save_document(record)
    await db.flush()
    reloaded = FilesystemDatabase(tmp_path, read_only=True)
    loaded = await load_documents_from_database(reloaded, {doc.sha256})
    assert len(loaded) == 1
    return loaded[0]


# ===========================================================================
# BUG: description "" ↔ None asymmetry
# ===========================================================================


class TestDescriptionNormalization:
    """description is always str (never None). None is normalized to '' at construction."""

    def test_empty_string_stays_empty(self) -> None:
        doc = RoundTripDoc(name="test.txt", content=b"data", description="")
        assert doc.description == ""

    def test_none_normalized_to_empty(self) -> None:
        doc = RoundTripDoc(name="test.txt", content=b"data", description=None)
        assert doc.description == ""

    def test_default_is_empty(self) -> None:
        doc = RoundTripDoc(name="test.txt", content=b"data")
        assert doc.description == ""

    def test_nonempty_preserved(self) -> None:
        doc = RoundTripDoc(name="test.txt", content=b"data", description="important")
        assert doc.description == "important"

    def test_roundtrip_empty_preserved(self) -> None:
        doc = RoundTripDoc(name="test.txt", content=b"data", description="")
        hydrated = _hydrate_from_record(doc)
        assert hydrated.description == ""

    def test_roundtrip_nonempty_preserved(self) -> None:
        doc = RoundTripDoc(name="test.txt", content=b"data", description="important")
        hydrated = _hydrate_from_record(doc)
        assert hydrated.description == "important"

    async def test_memory_roundtrip_description_preserved(self) -> None:
        doc = RoundTripDoc(name="test.txt", content=b"data", description="preserved")
        loaded = await _roundtrip_memory(doc)
        assert loaded.description == "preserved"

    async def test_filesystem_roundtrip_description_preserved(self, tmp_path: Path) -> None:
        doc = RoundTripDoc(name="test.txt", content=b"data", description="preserved")
        loaded = await _roundtrip_filesystem(doc, tmp_path)
        assert loaded.description == "preserved"


# ===========================================================================
# BUG: attachment description "" ↔ None asymmetry
# ===========================================================================


class TestAttachmentDescriptionNormalization:
    """Attachment description is always str (never None). None normalized to ''."""

    def test_empty_string_stays_empty(self) -> None:
        att = Attachment(name="img.png", content=MINIMAL_PNG, description="")
        assert att.description == ""

    def test_none_normalized_to_empty(self) -> None:
        att = Attachment(name="img.png", content=MINIMAL_PNG, description=None)
        assert att.description == ""

    def test_nonempty_preserved(self) -> None:
        att = Attachment(name="img.png", content=MINIMAL_PNG, description="screenshot")
        assert att.description == "screenshot"

    def test_roundtrip_empty_preserved(self) -> None:
        att = Attachment(name="img.png", content=MINIMAL_PNG, description="")
        doc = RoundTripDoc(name="test.txt", content=b"data", attachments=(att,))
        hydrated = _hydrate_from_record(doc)
        assert hydrated.attachments[0].description == ""

    def test_roundtrip_nonempty_preserved(self) -> None:
        att = Attachment(name="img.png", content=MINIMAL_PNG, description="screenshot")
        doc = RoundTripDoc(name="test.txt", content=b"data", attachments=(att,))
        hydrated = _hydrate_from_record(doc)
        assert hydrated.attachments[0].description == "screenshot"


# ===========================================================================
# BUG: row_to_blob corrupts non-UTF-8 binary from ClickHouse
# ===========================================================================


class TestRowToBlobHexDecoding:
    """row_to_blob now receives hex-encoded strings via BLOB_SELECT_COLUMNS
    and decodes them with bytes.fromhex() to recover original binary."""

    def test_hex_string_decoded_to_original_bytes(self) -> None:
        """Hex-encoded content is correctly decoded back to original binary."""
        hex_str = WEBP_LIKE.hex()
        blob = row_to_blob(("sha_test", hex_str))
        assert blob.content == WEBP_LIKE

    def test_png_hex_decoded_correctly(self) -> None:
        """PNG binary survives hex encode → decode round-trip."""
        hex_str = MINIMAL_PNG.hex()
        blob = row_to_blob(("sha_test", hex_str))
        assert blob.content == MINIMAL_PNG

    def test_raw_bytes_pass_through(self) -> None:
        """When content is already bytes (tests, direct construction), no conversion."""
        blob = row_to_blob(("sha_test", WEBP_LIKE))
        assert blob.content == WEBP_LIKE

    def test_text_content_hex_decoded_correctly(self) -> None:
        """Text content hex-encoded by ClickHouse is decoded correctly."""
        text_bytes = "Hello 🌍 world".encode()
        hex_str = text_bytes.hex()
        blob = row_to_blob(("sha_test", hex_str))
        assert blob.content == text_bytes

    def test_all_byte_values_hex_decoded(self) -> None:
        """Every possible byte value survives hex encode → decode."""
        hex_str = ALL_BYTES.hex()
        blob = row_to_blob(("sha_test", hex_str))
        assert blob.content == ALL_BYTES
        assert len(blob.content) == 256

    def test_content_sha256_preserved_after_hex_decode(self) -> None:
        """Content SHA256 matches after hex decode."""
        original_sha = compute_content_sha256(MINIMAL_PNG)
        hex_str = MINIMAL_PNG.hex()
        blob = row_to_blob(("sha_test", hex_str))
        assert compute_content_sha256(blob.content) == original_sha


class TestBlobCorruptionDetectedByIntegrityCheck:
    """Wrong content is caught by the integrity check in hydrate_document."""

    def test_wrong_primary_content_raises(self) -> None:
        doc = RoundTripDoc(name="image.png", content=MINIMAL_PNG)
        record = document_to_record(doc)
        hydrated = HydratedDocument(record=record, content=b"wrong content", attachment_contents={})
        with pytest.raises(ValueError, match="integrity check failed"):
            hydrate_document(RoundTripDoc, hydrated)

    def test_wrong_attachment_content_raises(self) -> None:
        att = Attachment(name="screenshot.webp", content=WEBP_LIKE)
        doc = RoundTripDoc(name="report.txt", content=b"text", attachments=(att,))
        record = document_to_record(doc)
        blobs = document_to_blobs(doc)
        att_blob = blobs[1]
        hydrated = HydratedDocument(
            record=record,
            content=blobs[0].content,
            attachment_contents={att_blob.content_sha256: b"wrong attachment"},
        )
        with pytest.raises(ValueError, match="integrity check failed"):
            hydrate_document(RoundTripDoc, hydrated)


# ===========================================================================
# BUG: hydrate_document has no integrity verification
# ===========================================================================


class TestHydrationIntegrityCheck:
    """hydrate_document raises ValueError on sha256 mismatch after the fix."""

    def test_tampered_content_raises(self) -> None:
        doc = RoundTripDoc(name="test.txt", content=b"original")
        record = document_to_record(doc)
        hydrated = HydratedDocument(record=record, content=b"TAMPERED", attachment_contents={})
        with pytest.raises(ValueError, match="integrity check failed"):
            hydrate_document(RoundTripDoc, hydrated)

    def test_tampered_attachment_raises(self) -> None:
        att = Attachment(name="a.txt", content=b"real")
        doc = RoundTripDoc(name="test.txt", content=b"main", attachments=(att,))
        record = document_to_record(doc)
        blobs = document_to_blobs(doc)
        att_blob = blobs[1]
        hydrated = HydratedDocument(
            record=record,
            content=blobs[0].content,
            attachment_contents={att_blob.content_sha256: b"FAKE"},
        )
        with pytest.raises(ValueError, match="integrity check failed"):
            hydrate_document(RoundTripDoc, hydrated)


# ===========================================================================
# Positive: sha256 preserved through correct round-trip (Memory backend)
# ===========================================================================


class TestMemoryRoundtripSha256:
    """Verify sha256 is preserved through MemoryDatabase for various document shapes."""

    async def test_text_only(self) -> None:
        doc = RoundTripDoc(name="test.txt", content=b"hello world")
        loaded = await _roundtrip_memory(doc)
        assert loaded.sha256 == doc.sha256

    async def test_empty_content(self) -> None:
        doc = RoundTripDoc(name="empty.txt", content=b"")
        loaded = await _roundtrip_memory(doc)
        assert loaded.sha256 == doc.sha256

    async def test_unicode_content(self) -> None:
        doc = RoundTripDoc(name="unicode.txt", content="Hello 🌍 世界".encode())
        loaded = await _roundtrip_memory(doc)
        assert loaded.sha256 == doc.sha256

    async def test_with_derived_from(self) -> None:
        doc = RoundTripDoc(name="test.txt", content=b"x", derived_from=("https://a.com", "https://b.com"))
        loaded = await _roundtrip_memory(doc)
        assert loaded.sha256 == doc.sha256

    async def test_with_triggered_by(self) -> None:
        source = RoundTripDoc(name="src.txt", content=b"src")
        doc = RoundTripDoc(name="test.txt", content=b"x", triggered_by=(DocumentSha256(source.sha256),))
        loaded = await _roundtrip_memory(doc)
        assert loaded.sha256 == doc.sha256

    async def test_with_both_provenance(self) -> None:
        source = RoundTripDoc(name="src.txt", content=b"src")
        doc = RoundTripDoc(
            name="test.txt",
            content=b"full provenance",
            derived_from=("https://example.com",),
            triggered_by=(DocumentSha256(source.sha256),),
        )
        loaded = await _roundtrip_memory(doc)
        assert loaded.sha256 == doc.sha256

    async def test_single_text_attachment(self) -> None:
        att = Attachment(name="note.txt", content=b"attachment text", description="a note")
        doc = RoundTripDoc(name="main.txt", content=b"main content", attachments=(att,))
        loaded = await _roundtrip_memory(doc)
        assert loaded.sha256 == doc.sha256
        assert loaded.attachments[0].content == att.content

    async def test_multiple_text_attachments(self) -> None:
        atts = (
            Attachment(name="a.txt", content=b"aaa"),
            Attachment(name="b.txt", content=b"bbb"),
            Attachment(name="c.txt", content=b"ccc"),
        )
        doc = RoundTripDoc(name="multi.txt", content=b"main", attachments=atts)
        loaded = await _roundtrip_memory(doc)
        assert loaded.sha256 == doc.sha256

    async def test_binary_attachment_text_primary(self) -> None:
        """Text primary content with binary (PNG) attachment via MemoryDatabase."""
        att = Attachment(name="screenshot.png", content=MINIMAL_PNG)
        doc = RoundTripDoc(name="report.md", content=b"# Report", attachments=(att,))
        loaded = await _roundtrip_memory(doc)
        assert loaded.sha256 == doc.sha256
        assert loaded.attachments[0].content == MINIMAL_PNG

    async def test_complex_document(self) -> None:
        """Complex document with all fields populated."""
        source = RoundTripDoc(name="origin.txt", content=b"origin")
        atts = (
            Attachment(name="img.png", content=MINIMAL_PNG, description="screenshot"),
            Attachment(name="data.txt", content=b"supplementary data"),
        )
        doc = RoundTripDoc(
            name="complex.md",
            content=b"# Complex document with everything",
            description="A comprehensive test document",
            summary="complex doc summary",
            derived_from=("https://example.com/source",),
            triggered_by=(DocumentSha256(source.sha256),),
            attachments=atts,
        )
        loaded = await _roundtrip_memory(doc)
        assert loaded.sha256 == doc.sha256
        assert loaded.content == doc.content
        assert len(loaded.attachments) == 2
        assert loaded.attachments[0].content == MINIMAL_PNG


# ===========================================================================
# Positive: sha256 preserved through Filesystem backend
# ===========================================================================


class TestFilesystemRoundtripSha256:
    """Verify sha256 is preserved through FilesystemDatabase."""

    async def test_text_only(self, tmp_path: Path) -> None:
        doc = RoundTripDoc(name="test.txt", content=b"hello world")
        loaded = await _roundtrip_filesystem(doc, tmp_path)
        assert loaded.sha256 == doc.sha256

    async def test_with_provenance(self, tmp_path: Path) -> None:
        source = RoundTripDoc(name="src.txt", content=b"src")
        doc = RoundTripDoc(
            name="test.txt",
            content=b"derived",
            derived_from=("https://a.com",),
            triggered_by=(DocumentSha256(source.sha256),),
        )
        loaded = await _roundtrip_filesystem(doc, tmp_path)
        assert loaded.sha256 == doc.sha256

    async def test_text_attachment(self, tmp_path: Path) -> None:
        att = Attachment(name="note.txt", content=b"attachment text", description="desc")
        doc = RoundTripDoc(name="main.txt", content=b"main", attachments=(att,))
        loaded = await _roundtrip_filesystem(doc, tmp_path)
        assert loaded.sha256 == doc.sha256
        assert loaded.attachments[0].content == att.content

    async def test_binary_attachment(self, tmp_path: Path) -> None:
        """Binary attachment (PNG) survives filesystem round-trip."""
        att = Attachment(name="screenshot.png", content=MINIMAL_PNG)
        doc = RoundTripDoc(name="report.md", content=b"# Report", attachments=(att,))
        loaded = await _roundtrip_filesystem(doc, tmp_path)
        assert loaded.sha256 == doc.sha256
        assert loaded.attachments[0].content == MINIMAL_PNG

    async def test_complex_document(self, tmp_path: Path) -> None:
        source = RoundTripDoc(name="origin.txt", content=b"origin")
        atts = (
            Attachment(name="img.png", content=MINIMAL_PNG, description="screenshot"),
            Attachment(name="extra.txt", content=b"extra"),
        )
        doc = RoundTripDoc(
            name="complex.md",
            content=b"# Complex",
            description="test doc",
            derived_from=("https://example.com",),
            triggered_by=(DocumentSha256(source.sha256),),
            attachments=atts,
        )
        loaded = await _roundtrip_filesystem(doc, tmp_path)
        assert loaded.sha256 == doc.sha256


# ===========================================================================
# ClickHouse integration: binary blob round-trip
# ===========================================================================


@pytest.fixture(scope="module")
def clickhouse_container(require_docker):
    from testcontainers.clickhouse import ClickHouseContainer

    container = ClickHouseContainer(port=8123)
    container.with_exposed_ports(8123)
    with container:
        yield container


@pytest.fixture(scope="module")
def clickhouse_settings(clickhouse_container) -> Settings:
    host = clickhouse_container.get_container_host_ip()
    port = int(clickhouse_container.get_exposed_port(8123))
    return Settings(
        clickhouse_host=host,
        clickhouse_port=port,
        clickhouse_database=clickhouse_container.dbname,
        clickhouse_user=clickhouse_container.username,
        clickhouse_password=clickhouse_container.password,
        clickhouse_secure=False,
        clickhouse_connect_timeout=30,
        clickhouse_send_receive_timeout=60,
    )


@pytest.fixture(autouse=True)
def _reset_schema():
    from ai_pipeline_core.database.clickhouse._connection import reset_schema_check

    reset_schema_check()
    yield
    reset_schema_check()


@pytest.fixture
def ch_database(clickhouse_settings):
    from ai_pipeline_core.database.clickhouse._backend import ClickHouseDatabase

    return ClickHouseDatabase(settings=clickhouse_settings)


@pytest.mark.clickhouse
class TestClickHouseBlobRoundtrip:
    """Prove binary blobs are corrupted through ClickHouse save/load."""

    async def test_text_blob_roundtrip(self, ch_database) -> None:
        content = b"Hello world, plain text"
        sha = compute_content_sha256(content)
        await ch_database.save_blob(_BlobRecord(content_sha256=sha, content=content))
        loaded = await ch_database.get_blob(sha)
        assert loaded is not None
        assert loaded.content == content

    async def test_binary_png_blob_roundtrip(self, ch_database) -> None:
        """PNG blob round-trip through ClickHouse — fails if driver hex-encodes."""
        sha = compute_content_sha256(MINIMAL_PNG)
        await ch_database.save_blob(_BlobRecord(content_sha256=sha, content=MINIMAL_PNG))
        loaded = await ch_database.get_blob(sha)
        assert loaded is not None
        assert loaded.content == MINIMAL_PNG, (
            f"Binary blob corrupted: original {len(MINIMAL_PNG)} bytes, loaded {len(loaded.content)} bytes, first 16: {loaded.content[:16]!r}"
        )

    async def test_binary_webp_blob_roundtrip(self, ch_database) -> None:
        """WebP-like blob round-trip — the exact pattern that fails in production."""
        sha = compute_content_sha256(WEBP_LIKE)
        await ch_database.save_blob(_BlobRecord(content_sha256=sha, content=WEBP_LIKE))
        loaded = await ch_database.get_blob(sha)
        assert loaded is not None
        assert loaded.content == WEBP_LIKE

    async def test_all_byte_values_blob_roundtrip(self, ch_database) -> None:
        """Blob with every byte value 0x00-0xFF — maximally adversarial for encoding."""
        sha = compute_content_sha256(ALL_BYTES)
        await ch_database.save_blob(_BlobRecord(content_sha256=sha, content=ALL_BYTES))
        loaded = await ch_database.get_blob(sha)
        assert loaded is not None
        assert loaded.content == ALL_BYTES, f"All-bytes blob corrupted: original 256 bytes, loaded {len(loaded.content)} bytes"

    async def test_binary_blob_content_sha256_preserved(self, ch_database) -> None:
        """Content SHA256 must match after round-trip, not just content bytes."""
        sha = compute_content_sha256(MINIMAL_PNG)
        await ch_database.save_blob(_BlobRecord(content_sha256=sha, content=MINIMAL_PNG))
        loaded = await ch_database.get_blob(sha)
        assert loaded is not None
        recomputed = compute_content_sha256(loaded.content)
        assert recomputed == sha, f"Content SHA256 mismatch: stored={sha}, recomputed={recomputed}"


@pytest.mark.clickhouse
class TestClickHouseDocumentRoundtrip:
    """End-to-end document round-trip through ClickHouse with sha256 verification."""

    async def _roundtrip_ch(self, doc: Document, ch_database) -> Document:
        record = document_to_record(doc)
        blobs = document_to_blobs(doc)
        await ch_database.save_blob_batch(blobs)
        await ch_database.save_document(record)
        loaded = await load_documents_from_database(ch_database, {doc.sha256})
        assert len(loaded) == 1
        return loaded[0]

    async def test_text_document(self, ch_database) -> None:
        doc = RoundTripDoc(name="test.txt", content=b"hello world")
        loaded = await self._roundtrip_ch(doc, ch_database)
        assert loaded.sha256 == doc.sha256

    async def test_document_with_binary_attachment(self, ch_database) -> None:
        """Document with PNG attachment — the exact production failure pattern."""
        att = Attachment(name="screenshot.png", content=MINIMAL_PNG, description="Page screenshot")
        doc = RoundTripDoc(
            name="report.md",
            content=b"# Report with screenshot",
            description="Fetched content",
            attachments=(att,),
        )
        loaded = await self._roundtrip_ch(doc, ch_database)
        assert loaded.sha256 == doc.sha256, f"Document sha256 mismatch: original={doc.sha256}, loaded={loaded.sha256}"
        assert loaded.attachments[0].content == MINIMAL_PNG, f"Attachment content corrupted: {len(MINIMAL_PNG)} → {len(loaded.attachments[0].content)}"

    async def test_document_with_webp_attachment(self, ch_database) -> None:
        att = Attachment(name="screenshot.webp", content=WEBP_LIKE)
        doc = RoundTripDoc(name="page.md", content=b"# Page", attachments=(att,))
        loaded = await self._roundtrip_ch(doc, ch_database)
        assert loaded.sha256 == doc.sha256
        assert loaded.attachments[0].content == WEBP_LIKE

    async def test_document_with_all_bytes_attachment(self, ch_database) -> None:
        att = Attachment(name="data.bin", content=ALL_BYTES)
        doc = RoundTripDoc(name="carrier.txt", content=b"text carrier", attachments=(att,))
        loaded = await self._roundtrip_ch(doc, ch_database)
        assert loaded.sha256 == doc.sha256
        assert loaded.attachments[0].content == ALL_BYTES

    async def test_complex_document_with_mixed_attachments(self, ch_database) -> None:
        """Complex document: text primary + text attachment + binary attachment."""
        source = RoundTripDoc(name="origin.txt", content=b"origin")
        atts = (
            Attachment(name="notes.txt", content=b"text attachment", description="notes"),
            Attachment(name="screenshot.png", content=MINIMAL_PNG, description="screenshot"),
        )
        doc = RoundTripDoc(
            name="complex.md",
            content=b"# Complex document",
            description="A test document",
            derived_from=("https://example.com",),
            triggered_by=(DocumentSha256(source.sha256),),
            attachments=atts,
        )
        loaded = await self._roundtrip_ch(doc, ch_database)
        assert loaded.sha256 == doc.sha256
        assert loaded.attachments[0].content == b"text attachment"
        assert loaded.attachments[1].content == MINIMAL_PNG
