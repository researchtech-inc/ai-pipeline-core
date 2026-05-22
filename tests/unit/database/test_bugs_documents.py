"""Tests for document persistence fixes.

Covers:
- Insert-once semantics for documents
- Missing attachment blob detection raises explicit error
"""

import pytest

from ai_pipeline_core.database._documents import _attachment_contents_for_record, document_to_record
from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.database._types import DocumentRecord, _BlobRecord
from ai_pipeline_core.documents.document import Document


class SampleDoc(Document):
    """Sample document for tests."""

    publicly_visible = False


async def test_save_document_is_insert_once() -> None:
    """Re-saving the same document SHA is a no-op — first record wins."""
    db = _MemoryDatabase()
    doc = SampleDoc(name="test", content=b"hello")

    record1 = document_to_record(doc)
    await db.save_document(record1)

    record2 = document_to_record(doc)
    await db.save_document(record2)

    stored = await db.get_document(record1.document_sha256)
    assert stored == record1


async def test_save_blob_is_insert_once() -> None:
    """Re-saving the same blob SHA is a no-op — first blob wins."""
    db = _MemoryDatabase()
    blob1 = _BlobRecord(content_sha256="sha1", content=b"data")
    await db.save_blob(blob1)

    blob2 = _BlobRecord(content_sha256="sha1", content=b"data")
    await db.save_blob(blob2)

    stored = await db.get_blob("sha1")
    assert stored == blob1


async def test_update_document_summary_changes_only_summary() -> None:
    """update_document_summary must only change the summary field."""
    db = _MemoryDatabase()
    doc = SampleDoc(name="test", content=b"hello")
    record = document_to_record(doc)
    await db.save_document(record)

    await db.update_document_summary(record.document_sha256, "new summary")
    stored = await db.get_document(record.document_sha256)
    assert stored is not None
    assert stored.summary == "new summary"
    assert stored.name == record.name
    assert stored.content_sha256 == record.content_sha256


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


def test_attachment_contents_returns_all_when_present() -> None:
    """All blobs present — returns complete dict."""
    record = DocumentRecord(
        document_sha256="doc_sha",
        content_sha256="content_sha",
        document_type="SampleDoc",
        name="test",
        attachment_names=("att1.txt",),
        attachment_descriptions=("",),
        attachment_content_sha256s=("att_sha1",),
        attachment_mime_types=("text/plain",),
        attachment_size_bytes=(10,),
    )
    blobs = {"att_sha1": _BlobRecord(content_sha256="att_sha1", content=b"data1")}
    result = _attachment_contents_for_record(record, blobs)
    assert result == {"att_sha1": b"data1"}
