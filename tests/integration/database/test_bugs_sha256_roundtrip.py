"""ClickHouse integration tests for document SHA256 round-trip integrity."""

import base64

import pytest

from ai_pipeline_core.database._documents import document_to_blobs, document_to_record, load_documents_from_database
from ai_pipeline_core.database._types import _BlobRecord
from ai_pipeline_core.documents._context import DocumentSha256
from ai_pipeline_core.documents._hashing import compute_content_sha256
from ai_pipeline_core.documents.attachment import Attachment
from ai_pipeline_core.documents.document import Document
from ai_pipeline_core.settings import Settings

MINIMAL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
WEBP_LIKE = b"RIFF\x76\xbc\x00\x00WEBPVP8 " + bytes(range(128, 256))
ALL_BYTES = bytes(range(256))


class RoundTripDoc(Document):
    """Document subclass for ClickHouse round-trip tests."""


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
async def ch_database(clickhouse_settings):
    from ai_pipeline_core.database.clickhouse._backend import ClickHouseDatabase

    db = ClickHouseDatabase(settings=clickhouse_settings)
    try:
        yield db
    finally:
        # 1.0.x native aiohttp sessions leak loudly if not explicitly closed.
        await db.shutdown()


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
        """PNG blob round-trip through ClickHouse fails if driver hex-encodes."""
        sha = compute_content_sha256(MINIMAL_PNG)
        await ch_database.save_blob(_BlobRecord(content_sha256=sha, content=MINIMAL_PNG))
        loaded = await ch_database.get_blob(sha)
        assert loaded is not None
        assert loaded.content == MINIMAL_PNG, (
            f"Binary blob corrupted: original {len(MINIMAL_PNG)} bytes, "
            f"loaded {len(loaded.content)} bytes, first 16: {loaded.content[:16]!r}"
        )

    async def test_binary_webp_blob_roundtrip(self, ch_database) -> None:
        """WebP-like blob round-trip covers the exact production failure pattern."""
        sha = compute_content_sha256(WEBP_LIKE)
        await ch_database.save_blob(_BlobRecord(content_sha256=sha, content=WEBP_LIKE))
        loaded = await ch_database.get_blob(sha)
        assert loaded is not None
        assert loaded.content == WEBP_LIKE

    async def test_all_byte_values_blob_roundtrip(self, ch_database) -> None:
        """Blob with every byte value 0x00-0xFF is maximally adversarial for encoding."""
        sha = compute_content_sha256(ALL_BYTES)
        await ch_database.save_blob(_BlobRecord(content_sha256=sha, content=ALL_BYTES))
        loaded = await ch_database.get_blob(sha)
        assert loaded is not None
        assert loaded.content == ALL_BYTES, (
            f"All-bytes blob corrupted: original 256 bytes, loaded {len(loaded.content)} bytes"
        )

    async def test_binary_blob_content_sha256_preserved(self, ch_database) -> None:
        """Content SHA256 must match after round-trip, not just content bytes."""
        sha = compute_content_sha256(MINIMAL_PNG)
        await ch_database.save_blob(_BlobRecord(content_sha256=sha, content=MINIMAL_PNG))
        loaded = await ch_database.get_blob(sha)
        assert loaded is not None
        recomputed = compute_content_sha256(loaded.content)
        assert recomputed == sha, f"Content SHA256 mismatch: stored={sha}, recomputed={recomputed}"


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
        """Document with PNG attachment covers the exact production failure pattern."""
        att = Attachment(name="screenshot.png", content=MINIMAL_PNG, description="Page screenshot")
        doc = RoundTripDoc(
            name="report.md",
            content=b"# Report with screenshot",
            description="Fetched content",
            attachments=(att,),
        )
        loaded = await self._roundtrip_ch(doc, ch_database)
        assert loaded.sha256 == doc.sha256, f"Document sha256 mismatch: original={doc.sha256}, loaded={loaded.sha256}"
        assert loaded.attachments[0].content == MINIMAL_PNG, (
            f"Attachment content corrupted: {len(MINIMAL_PNG)} -> {len(loaded.attachments[0].content)}"
        )

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
