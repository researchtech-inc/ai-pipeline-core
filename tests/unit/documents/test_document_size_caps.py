"""Tests for document field size caps (URI, description, summary).

Proves that oversized metadata is rejected at validation time,
preventing AI agents from stuffing base64 data into URIs or
writing unbounded descriptions/summaries.
"""

import pytest

from ai_pipeline_core.documents.document import (
    Document,
    MAX_DESCRIPTION_BYTES,
    MAX_EXTERNAL_SOURCE_URI_BYTES,
    MAX_SUMMARY_BYTES,
    MAX_TOTAL_DERIVED_FROM_BYTES,
)


class SizeCapDoc(Document):
    """Test document for size cap validation."""


class SourceDoc(Document):
    """Source document for provenance."""


def _root(name: str = "test.txt", content: str = "hello") -> SourceDoc:
    return SourceDoc.create_root(name=name, content=content, reason="test")


class TestExternalSourceURICap:
    """External source URIs must be under 8 KB each."""

    def test_normal_url_accepted(self) -> None:
        doc = SizeCapDoc.create_external(
            name="page.html",
            content=b"<html>ok</html>",
            from_sources=["https://example.com/page"],
        )
        assert len(doc.derived_from) == 1

    def test_oversized_uri_rejected(self) -> None:
        huge_uri = "https://example.com/data?payload=" + "A" * (MAX_EXTERNAL_SOURCE_URI_BYTES + 1)
        with pytest.raises(ValueError, match=r"exceeds.*limit"):
            SizeCapDoc.create_external(
                name="page.html",
                content=b"<html>ok</html>",
                from_sources=[huge_uri],
            )

    def test_uri_just_under_limit_accepted(self) -> None:
        uri = "https://x.com/" + "A" * (MAX_EXTERNAL_SOURCE_URI_BYTES - 20)
        doc = SizeCapDoc.create_external(
            name="page.html",
            content=b"ok",
            from_sources=[uri],
        )
        assert len(doc.derived_from) == 1

    def test_base64_in_uri_rejected(self) -> None:
        """Real-world case: AI agent puts base64 data in URI."""
        base64_uri = "data://base64," + "A" * 10_000
        with pytest.raises(ValueError, match=r"exceeds.*limit"):
            SizeCapDoc.create_external(
                name="page.html",
                content=b"ok",
                from_sources=[base64_uri],
            )


class TestTotalDerivedFromCap:
    """Total combined size of all derived_from entries must be under 200 KB."""

    def test_many_small_sources_accepted(self) -> None:
        sources = [f"https://example.com/page-{i}" for i in range(100)]
        doc = SizeCapDoc.create_external(
            name="page.html",
            content=b"ok",
            from_sources=sources,
        )
        assert len(doc.derived_from) == 100

    def test_total_exceeds_limit_rejected(self) -> None:
        single_size = MAX_EXTERNAL_SOURCE_URI_BYTES - 100
        count = (MAX_TOTAL_DERIVED_FROM_BYTES // single_size) + 2
        sources = [f"https://example.com/{'A' * single_size}" for _ in range(count)]
        with pytest.raises(ValueError, match=r"Total derived_from size"):
            SizeCapDoc.create_external(
                name="page.html",
                content=b"ok",
                from_sources=sources,
            )


class TestDescriptionCap:
    """Description must be under 50 KB."""

    def test_normal_description_accepted(self) -> None:
        doc = SizeCapDoc.create_root(
            name="test.txt",
            content="hello",
            reason="test",
            description="A short description",
        )
        assert doc.description == "A short description"

    def test_oversized_description_rejected(self) -> None:
        huge_desc = "X" * (MAX_DESCRIPTION_BYTES + 1)
        with pytest.raises(ValueError, match=r"exceeds.*limit"):
            SizeCapDoc.create_root(
                name="test.txt",
                content="hello",
                reason="test",
                description=huge_desc,
            )

    def test_description_just_under_limit_accepted(self) -> None:
        desc = "X" * MAX_DESCRIPTION_BYTES
        doc = SizeCapDoc.create_root(
            name="test.txt",
            content="hello",
            reason="test",
            description=desc,
        )
        assert len(doc.description) == MAX_DESCRIPTION_BYTES


class TestSummaryCap:
    """Summary must be under 1 KB."""

    def test_normal_summary_accepted(self) -> None:
        doc = SizeCapDoc.create_root(
            name="test.txt",
            content="hello",
            reason="test",
            summary="Short summary",
        )
        assert doc.summary == "Short summary"

    def test_oversized_summary_rejected(self) -> None:
        huge_summary = "X" * (MAX_SUMMARY_BYTES + 1)
        with pytest.raises(ValueError, match=r"exceeds.*limit"):
            SizeCapDoc.create_root(
                name="test.txt",
                content="hello",
                reason="test",
                summary=huge_summary,
            )

    def test_empty_summary_accepted(self) -> None:
        doc = SizeCapDoc.create_root(
            name="test.txt",
            content="hello",
            reason="test",
            summary="",
        )
        assert doc.summary == ""


class TestDescriptionAffectsIdentity:
    """description is part of sha256; summary is not."""

    def test_different_description_different_sha256(self) -> None:
        doc_a = SizeCapDoc.create_root(name="test.txt", content="same", reason="test", description="version A")
        doc_b = SizeCapDoc.create_root(name="test.txt", content="same", reason="test", description="version B")
        assert doc_a.sha256 != doc_b.sha256

    def test_different_summary_same_sha256(self) -> None:
        doc_a = SizeCapDoc.create_root(name="test.txt", content="same", reason="test", summary="summary A")
        doc_b = SizeCapDoc.create_root(name="test.txt", content="same", reason="test", summary="summary B")
        assert doc_a.sha256 == doc_b.sha256

    def test_none_description_equals_empty_description(self) -> None:
        doc_a = SizeCapDoc.create_root(name="test.txt", content="same", reason="test", description=None)
        doc_b = SizeCapDoc.create_root(name="test.txt", content="same", reason="test", description="")
        assert doc_a.sha256 == doc_b.sha256
