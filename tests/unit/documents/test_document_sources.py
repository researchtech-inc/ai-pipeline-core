"""Tests for Document derived_from field and methods."""

import pytest

from ai_pipeline_core.documents import Document
from ai_pipeline_core.documents.attachment import Attachment


class SampleFlowDoc(Document):
    """Sample flow document for testing."""


@pytest.fixture(autouse=True)
def _suppress_registration():
    return


class TestDocumentDerivedFrom:
    """Test Document derived_from functionality."""

    def test_create_document_with_derived_from(self):
        """Test creating document with derived_from."""
        derived_from = [
            "https://example.com",
            "P3AEMA2PSYILKFYVBUALJLMIYW",  # Valid SHA256
        ]

        doc = SampleFlowDoc(name="test.txt", content=b"test content", derived_from=derived_from)

        assert len(doc.derived_from) == 2
        assert doc.derived_from[0] == "https://example.com"
        assert doc.derived_from[1] == "P3AEMA2PSYILKFYVBUALJLMIYW"

    def test_default_empty_derived_from(self):
        """Test that derived_from defaults to empty tuple."""
        doc = SampleFlowDoc.create_root(name="test.txt", content="test", reason="test input")
        assert doc.derived_from == ()

    def test_with_derived_from_document(self):
        """Test adding a document as derived_from."""
        doc1 = SampleFlowDoc.create_root(name="source.txt", content="source data", reason="test input")

        # Create doc2 with doc1 as derived_from
        doc2 = SampleFlowDoc(name="derived.txt", content=b"derived data", derived_from=(doc1.sha256,))

        assert len(doc2.derived_from) == 1
        assert doc2.derived_from[0] == doc1.sha256

    def test_create_with_string_derived_from(self):
        """Test creating document with a string reference as derived_from."""
        doc = SampleFlowDoc(name="test.txt", content=b"test", derived_from=("https://data.source.com/file.csv",))

        assert len(doc.derived_from) == 1
        assert doc.derived_from[0] == "https://data.source.com/file.csv"

    def test_create_with_mixed_derived_from(self):
        """Test creating document with mixed derived_from types."""
        doc = SampleFlowDoc(
            name="test.txt",
            content=b"test",
            derived_from=(
                "https://example.com/manual-input",
                "P3AEMA2PSYILKFYVBUALJLMIYW",
            ),
        )

        assert len(doc.derived_from) == 2
        assert doc.derived_from[0] == "https://example.com/manual-input"
        assert doc.derived_from[1] == "P3AEMA2PSYILKFYVBUALJLMIYW"

    def test_create_with_duplicate_derived_from(self):
        """Test creating document with duplicate derived_from entries."""
        doc = SampleFlowDoc(name="test.txt", content=b"test", derived_from=("https://example.com/ref1", "https://example.com/ref1"))

        # Both entries are kept (no deduplication at creation)
        assert len(doc.derived_from) == 2

    def test_get_content_documents(self):
        """Test getting document hash entries from derived_from."""
        derived_from = [
            "P3AEMA2PSYILKFYVBUALJLMIYW",
            "https://example.com",
            "DSITTXMIGUJ5CHKJEVTW3IOQFY",
        ]

        doc = SampleFlowDoc(name="test.txt", content=b"test", derived_from=derived_from)

        hashes = doc.content_documents
        assert len(hashes) == 2
        assert "P3AEMA2PSYILKFYVBUALJLMIYW" in hashes
        assert "DSITTXMIGUJ5CHKJEVTW3IOQFY" in hashes

    def test_get_content_references(self):
        """Test getting reference entries from derived_from."""
        derived_from = [
            "P3AEMA2PSYILKFYVBUALJLMIYW",
            "https://example.com/ref1",
            "https://example.com/ref2",
        ]

        doc = SampleFlowDoc(name="test.txt", content=b"test", derived_from=derived_from)

        refs = doc.content_references
        assert len(refs) == 2
        assert "https://example.com/ref1" in refs
        assert "https://example.com/ref2" in refs

    def test_has_derived_from(self):
        """Test checking for specific derived_from entries."""
        doc1 = SampleFlowDoc.create_root(name="source.txt", content="source", reason="test input")
        doc2 = SampleFlowDoc.create_root(name="other.txt", content="other", reason="test input")

        doc = SampleFlowDoc(
            name="test.txt",
            content=b"test",
            derived_from=(
                doc1.sha256,
                "https://example.com/ref1",
            ),
        )

        # Check by document
        assert doc.has_derived_from(doc1)
        assert not doc.has_derived_from(doc2)

        # Check by string reference
        assert doc.has_derived_from("https://example.com/ref1")
        assert not doc.has_derived_from("https://example.com/ref2")

        # Check by SHA256 string
        assert doc.has_derived_from(doc1.sha256)
        assert not doc.has_derived_from(doc2.sha256)

    def test_serialize_with_derived_from(self):
        """Test serialization includes derived_from."""
        derived_from = [
            "https://example.com/ref1",
            "P3AEMA2PSYILKFYVBUALJLMIYW",
        ]

        doc = SampleFlowDoc(name="test.txt", content=b"test", derived_from=derived_from)

        data = doc.serialize_model()
        assert "derived_from" in data
        assert len(data["derived_from"]) == 2
        assert data["derived_from"][0] == "https://example.com/ref1"
        assert data["derived_from"][1] == "P3AEMA2PSYILKFYVBUALJLMIYW"

    def test_model_validate_with_derived_from(self):
        """Test deserialization includes derived_from."""
        data = {
            "name": "test.txt",
            "content": "test content",
            "derived_from": [
                "https://example.com/ref1",
                "P3AEMA2PSYILKFYVBUALJLMIYW",
            ],
        }

        doc = SampleFlowDoc.model_validate(data)
        assert len(doc.derived_from) == 2
        assert doc.derived_from[0] == "https://example.com/ref1"
        assert doc.derived_from[1] == "P3AEMA2PSYILKFYVBUALJLMIYW"

    def test_derived_from_immutable(self):
        """Test that derived_from tuple itself is immutable."""
        from pydantic import ValidationError

        doc = SampleFlowDoc(name="test.txt", content=b"test", derived_from=("https://example.com/ref1",))

        # Document is frozen, so we can't modify derived_from directly
        with pytest.raises(ValidationError, match="frozen"):
            doc.derived_from = []

    def test_concrete_document_with_derived_from(self):
        """Test that concrete Document subclass supports derived_from."""
        derived_from = ["https://example.com/temp-source"]
        doc = SampleFlowDoc(name="temp.txt", content=b"temporary", derived_from=derived_from)

        assert len(doc.derived_from) == 1
        assert doc.derived_from[0] == "https://example.com/temp-source"

    def test_low_entropy_hash_not_counted_as_document(self):
        """Test that low-entropy strings are not counted as document hashes."""
        derived_from = [
            "https://example.com/low-entropy-test",  # URL, not a document hash
            "https://example.com",
            "P3AEMA2PSYILKFYVBUALJLMIYW",  # Real hash
        ]

        doc = SampleFlowDoc(name="test.txt", content=b"test", derived_from=derived_from)

        # content_documents should only return the real hash
        hashes = doc.content_documents
        assert len(hashes) == 1
        assert hashes[0] == "P3AEMA2PSYILKFYVBUALJLMIYW"

        # content_references should return the low-entropy string and URL
        refs = doc.content_references
        assert len(refs) == 2
        assert "https://example.com/low-entropy-test" in refs
        assert "https://example.com" in refs

    def test_has_derived_from_invalid_type(self):
        """Test that has_derived_from raises TypeError for invalid types."""
        doc = SampleFlowDoc(name="test.txt", content=b"test", derived_from=("https://example.com/ref1",))

        # Invalid type should raise TypeError
        from typing import Any, cast

        with pytest.raises(TypeError, match="Invalid source type"):
            doc.has_derived_from(cast(Any, 123))  # Invalid: int

        with pytest.raises(TypeError, match="Invalid source type"):
            doc.has_derived_from(cast(Any, ["list"]))  # Invalid: list

        with pytest.raises(TypeError, match="Invalid source type"):
            doc.has_derived_from(cast(Any, {"dict": "value"}))  # Invalid: dict

    def test_empty_derived_from_methods(self):
        """Test methods behavior with empty derived_from."""
        doc = SampleFlowDoc.create_root(
            name="test.txt",
            content="test",
            reason="test input",
        )

        # All methods should work with empty derived_from
        assert doc.derived_from == ()
        assert doc.content_documents == ()
        assert doc.content_references == ()
        assert not doc.has_derived_from("anything")
        assert not doc.has_derived_from(doc)  # Even checking self returns False

    def test_derived_from_in_direct_constructor(self):
        """Test derived_from parameter in direct __init__ constructor."""
        derived_from = ("https://example.com/ref1", "https://example.com/ref2")
        doc = SampleFlowDoc(name="test.txt", content=b"test bytes", derived_from=derived_from)

        assert doc.derived_from == derived_from
        assert len(doc.content_references) == 2

    def test_multiple_identical_hashes(self):
        """Test handling of multiple identical document hashes."""
        doc1 = SampleFlowDoc.create_root(name="source.txt", content="source", reason="test input")

        # Create doc with duplicate hash in derived_from
        doc2 = SampleFlowDoc(name="derived.txt", content=b"derived", derived_from=(doc1.sha256, doc1.sha256, "https://example.com/other"))

        # All duplicates are kept
        assert len(doc2.derived_from) == 3
        hashes = doc2.content_documents
        assert len(hashes) == 2  # Both copies of the hash
        assert all(h == doc1.sha256 for h in hashes)

        # has_derived_from still works
        assert doc2.has_derived_from(doc1)
        assert doc2.has_derived_from(doc1.sha256)

    def test_self_referential_derived_from(self):
        """Test document referencing itself as derived_from."""
        doc = SampleFlowDoc.create_root(name="test.txt", content="test", reason="test input")

        # Create another doc that references itself (edge case)
        doc2 = SampleFlowDoc(
            name="self.txt",
            content=b"self-ref",
            derived_from=(doc.sha256,),  # Reference to first doc
        )

        # Add its own hash (would be unusual but should work)
        doc3 = SampleFlowDoc(name="self2.txt", content=b"self-ref2", derived_from=(doc2.sha256, "https://example.com/self-reference-note"))

        assert doc3.has_derived_from(doc2)
        assert len(doc3.content_documents) == 1

    def test_serialize_deserialize_preserves_derived_from(self):
        """Test full roundtrip serialization preserves derived_from."""
        derived_from = [
            "https://example.com/data",
            "P3AEMA2PSYILKFYVBUALJLMIYW",
            "https://example.com/local-file.csv",
        ]

        original = SampleFlowDoc(name="test.json", content=b'{"key": "value"}', description="Test doc", derived_from=derived_from)

        # Serialize and deserialize
        serialized = original.serialize_model()
        restored = SampleFlowDoc.model_validate(serialized)

        # Everything should match
        assert restored.name == original.name
        assert restored.content == original.content
        assert restored.description == original.description
        assert restored.derived_from == original.derived_from
        assert restored.sha256 == original.sha256  # Content-based, should match

    def test_derived_from_with_special_characters(self):
        """Test derived_from containing special characters."""
        derived_from = (
            "https://example.com/file with spaces.txt",
            "https://example.com/path/to/file.csv",
            "https://example.com/data?param=value&other=123",
            "https://example.com/unicode-źćąęł.txt",
            "https://example.com/tab\tseparated",
            "https://example.com/newline\nincluded",
        )

        doc = SampleFlowDoc(name="test.txt", content=b"test", derived_from=derived_from)

        # All entries should be preserved exactly
        assert doc.derived_from == derived_from
        refs = doc.content_references
        assert len(refs) == len(derived_from)
        for src in derived_from:
            assert src in refs


class TestDocumentSHA256WithAttachments:
    """Test SHA256 hashing accounts for attachments."""

    def test_sha256_deterministic_without_attachments(self):
        """SHA256 is deterministic for same name + content."""
        content = b"test content"
        doc1 = SampleFlowDoc(name="test.txt", content=content)
        doc2 = SampleFlowDoc(name="test.txt", content=content)
        assert doc1.sha256 == doc2.sha256
        assert len(doc1.sha256) == 26  # base32 SHA256 prefix

    def test_sha256_differs_with_attachment(self):
        """SHA256 changes when attachments are added."""
        content = b"test content"
        doc_without = SampleFlowDoc(name="test.txt", content=content)
        doc_with = SampleFlowDoc(
            name="test.txt",
            content=content,
            attachments=(Attachment(name="screenshot.png", content=b"\x89PNG"),),
        )
        assert doc_without.sha256 != doc_with.sha256

    def test_sha256_includes_attachment_name(self):
        """Changing attachment name changes the hash."""
        content = b"test content"
        att_content = b"\x89PNG"
        doc_a = SampleFlowDoc(
            name="test.txt",
            content=content,
            attachments=(Attachment(name="screenshot.png", content=att_content),),
        )
        doc_b = SampleFlowDoc(
            name="test.txt",
            content=content,
            attachments=(Attachment(name="error_page.png", content=att_content),),
        )
        assert doc_a.sha256 != doc_b.sha256

    def test_sha256_includes_attachment_content(self):
        """Changing attachment content changes the hash."""
        content = b"test content"
        doc_a = SampleFlowDoc(
            name="test.txt",
            content=content,
            attachments=(Attachment(name="file.bin", content=b"\x00\x01"),),
        )
        doc_b = SampleFlowDoc(
            name="test.txt",
            content=content,
            attachments=(Attachment(name="file.bin", content=b"\x02\x03"),),
        )
        assert doc_a.sha256 != doc_b.sha256
