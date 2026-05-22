"""Tests for document storage operations."""

from typing import ClassVar

import pytest

from ai_pipeline_core.database import DocumentRecord
from ai_pipeline_core.database._documents import store_document
from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.documents import Document


class StoreTestDoc(Document):
    """Document for store_document test."""

    publicly_visible: ClassVar[bool] = True


@pytest.mark.asyncio
async def test_publicly_visible_field_on_document_record() -> None:
    doc = DocumentRecord(
        document_sha256="doc-test",
        content_sha256="blob-test",
        document_type="TestDocument",
        name="test.md",
        mime_type="text/markdown",
        size_bytes=10,
        publicly_visible=True,
    )
    assert doc.publicly_visible is True

    doc_default = DocumentRecord(
        document_sha256="doc-test2",
        content_sha256="blob-test2",
        document_type="TestDocument",
        name="test.md",
        mime_type="text/markdown",
        size_bytes=10,
    )
    assert doc_default.publicly_visible is False


@pytest.mark.asyncio
async def test_store_document_roundtrip() -> None:
    db = _MemoryDatabase()
    doc = StoreTestDoc.create_root(reason="test", name="test.md", content="hello")
    await store_document(db, doc)

    record = await db.get_document(doc.sha256)
    assert record is not None
    assert record.document_type == "StoreTestDoc"
    assert record.publicly_visible is True

    blob = await db.get_blob(record.content_sha256)
    assert blob is not None
    assert blob.content == b"hello"
