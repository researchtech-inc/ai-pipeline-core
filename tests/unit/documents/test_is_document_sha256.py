"""Tests for _is_document_sha256 function."""

import hashlib
from base64 import b32encode

import pytest

from ai_pipeline_core.documents import Document
from ai_pipeline_core.documents.utils import _is_document_sha256


class _Sha256SampleDoc(Document):
    """Document type used for SHA256 validation tests."""


class _Sha256DerivedSampleDoc(Document):
    """Document type used for SHA256 derived-document integration test."""


@pytest.fixture(autouse=True)
def _suppress_registration():
    return


class TestIsDocumentSha256:
    """Test the _is_document_sha256 function."""

    def test_real_document_hash(self):
        """Test with a real document SHA256 hash."""

        # Create a real document and get its hash
        doc = _Sha256SampleDoc.create_root(name="test.txt", content="test content", reason="test input")
        assert _is_document_sha256(doc.sha256)

    def test_various_real_hashes(self):
        """Test with various real SHA256 hashes."""
        # Generate real hashes from different content
        test_contents = [
            b"Hello, World!",
            b"The quick brown fox jumps over the lazy dog",
            b"",  # Empty content
            b"1234567890",
            b"\x00\x01\x02\x03\x04",  # Binary content
        ]

        for content in test_contents:
            sha256_hash = b32encode(hashlib.sha256(content).digest()).decode("ascii").upper().rstrip("=")[:26]
            assert _is_document_sha256(sha256_hash), f"Failed for content: {content!r}"

    def test_insufficient_entropy(self):
        """Test that low-entropy strings are rejected."""
        # All same character - definitely not a real hash
        assert not _is_document_sha256("A" * 26)
        assert not _is_document_sha256("2" * 26)
        assert not _is_document_sha256("7" * 26)

        # Only 2 unique characters
        assert not _is_document_sha256("AB" * 13)

        # Only 3 unique characters
        assert not _is_document_sha256("ABC" * 8 + "AB")

        # 5 unique characters (just below threshold of 6)
        test_str = "ABCDE" * 5 + "A"  # 26 chars with 5 unique
        assert len(test_str) == 26
        assert len(set(test_str)) == 5
        assert not _is_document_sha256(test_str)

    def test_sufficient_entropy(self):
        """Test that strings with sufficient entropy are accepted."""
        # Exactly 6 unique characters (minimum threshold)
        test_str = "ABCDEF" * 4 + "AB"  # 26 chars with 6 unique
        assert len(test_str) == 26
        assert len(set(test_str)) == 6
        assert _is_document_sha256(test_str)

        # More than 6 unique characters
        test_str = "ABCDEFGHIJ23456" + "ABCDEFGHIJK"  # 26 chars with 15 unique
        assert len(test_str) == 26
        assert _is_document_sha256(test_str)

    def test_wrong_length(self):
        """Test that strings with wrong length are rejected."""
        # Too short
        assert not _is_document_sha256("ABC")
        assert not _is_document_sha256("A" * 25)

        # Too long
        assert not _is_document_sha256("A" * 27)
        assert not _is_document_sha256("ABCDEFGHIJKLMNOPQRSTUVWXYZ2")

        # Old 52-char format
        assert not _is_document_sha256("A" * 52)

        # With padding
        assert not _is_document_sha256("A" * 26 + "====")

    def test_invalid_characters(self):
        """Test that strings with invalid base32 characters are rejected."""
        # Lowercase letters (base32 is uppercase only)
        assert not _is_document_sha256("a" * 26)
        assert not _is_document_sha256("AbCdEfGhIjKlMnOpQrStUvWxYz")

        # Invalid digits (0, 1, 8, 9 are not in base32)
        assert not _is_document_sha256("0" * 26)
        assert not _is_document_sha256("1" * 26)
        assert not _is_document_sha256("8" * 26)
        assert not _is_document_sha256("9" * 26)

        # Mixed invalid characters
        test_str = "ABCDEFGH" + "0189" + "ABCDEFGHIJKLMN"  # Contains 0,1,8,9
        assert len(test_str) == 26
        assert not _is_document_sha256(test_str)

        # Special characters
        assert not _is_document_sha256("A" * 12 + "=" + "A" * 13)
        assert not _is_document_sha256("A" * 12 + "-" + "A" * 13)
        assert not _is_document_sha256("A" * 12 + "_" + "A" * 13)

    def test_valid_base32_characters(self):
        """Test that all valid base32 characters are accepted."""
        # Use all valid base32 characters (A-Z, 2-7)
        valid_chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
        test_str = valid_chars[:26]
        assert len(test_str) == 26
        assert _is_document_sha256(test_str)

    def test_edge_cases(self):
        """Test edge cases and special inputs."""
        # Non-string inputs
        assert not _is_document_sha256(None)  # type: ignore[arg-type]
        assert not _is_document_sha256(123)  # type: ignore[arg-type]
        assert not _is_document_sha256([])  # type: ignore[arg-type]
        assert not _is_document_sha256(b"A" * 26)  # type: ignore[arg-type]

        # Empty string
        assert not _is_document_sha256("")

        # Whitespace
        assert not _is_document_sha256(" " * 26)
        assert not _is_document_sha256("A" * 25 + " ")

    def test_integration_with_document_derived_from(self):
        """Test that it works correctly with document derived_from field."""

        # Create a document and use its hash
        doc = _Sha256DerivedSampleDoc.create_root(name="test.txt", content="integration test", reason="test input")

        # The hash should be valid
        assert _is_document_sha256(doc.sha256)

        # Create another document with the first as source
        doc2 = _Sha256DerivedSampleDoc(
            name="derived.txt",
            content=b"derived from first",
            derived_from=(doc.sha256, "https://example.com/manual-reference"),
        )

        # Check that the source is properly categorized
        doc_sources = doc2.content_documents
        ref_sources = doc2.content_references

        assert len(doc_sources) == 1
        assert doc_sources[0] == doc.sha256
        assert len(ref_sources) == 1
        assert ref_sources[0] == "https://example.com/manual-reference"

    def test_performance_characteristics(self):
        """Test that the function is efficient for typical use."""
        import time

        # Generate a real hash
        real_hash = b32encode(hashlib.sha256(b"performance test").digest()).decode("ascii").upper().rstrip("=")[:26]

        # Test should be very fast (sub-millisecond)
        start = time.perf_counter()
        for _ in range(1000):
            _is_document_sha256(real_hash)
        elapsed = time.perf_counter() - start

        # Should process 1000 hashes in well under a second
        assert elapsed < 0.1, f"Performance issue: 1000 checks took {elapsed:.3f}s"
