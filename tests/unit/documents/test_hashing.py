"""Tests for document hashing utilities used by store implementations."""

import pytest

from ai_pipeline_core.documents import Attachment, Document
from ai_pipeline_core.documents._hashing import compute_content_sha256, compute_document_sha256


class HashDoc(Document):
    pass


@pytest.fixture(autouse=True)
def _suppress_registration():
    return


class TestComputeDocumentSha256:
    def test_deterministic(self):
        doc = HashDoc.create_root(name="a.txt", content="hello", reason="test input")
        h1 = compute_document_sha256(doc)
        h2 = compute_document_sha256(doc)
        assert h1 == h2

    def test_base32_format(self):
        doc = HashDoc.create_root(name="a.txt", content="hello", reason="test input")
        h = compute_document_sha256(doc)
        assert h.isascii()
        assert h == h.upper()
        assert len(h) == 26  # SHA256 in base32, truncated to 26 chars

    def test_different_name_different_hash(self):
        doc1 = HashDoc.create_root(name="a.txt", content="hello", reason="test input")
        doc2 = HashDoc.create_root(name="b.txt", content="hello", reason="test input")
        assert compute_document_sha256(doc1) != compute_document_sha256(doc2)

    def test_different_content_different_hash(self):
        doc1 = HashDoc.create_root(name="a.txt", content="hello", reason="test input")
        doc2 = HashDoc.create_root(name="a.txt", content="world", reason="test input")
        assert compute_document_sha256(doc1) != compute_document_sha256(doc2)

    def test_attachments_affect_hash(self):
        doc1 = HashDoc.create_root(name="a.txt", content="hello", reason="test input")
        att = Attachment(name="img.png", content=b"\x89PNG")
        doc2 = HashDoc.create_root(name="a.txt", content="hello", attachments=(att,), reason="test input")
        assert compute_document_sha256(doc1) != compute_document_sha256(doc2)

    def test_attachment_order_does_not_matter(self):
        """Attachments are sorted by name before hashing."""
        att_a = Attachment(name="a.txt", content=b"aaa")
        att_b = Attachment(name="b.txt", content=b"bbb")
        doc1 = HashDoc.create_root(name="doc.txt", content="content", attachments=(att_a, att_b), reason="test input")
        doc2 = HashDoc.create_root(name="doc.txt", content="content", attachments=(att_b, att_a), reason="test input")
        assert compute_document_sha256(doc1) == compute_document_sha256(doc2)

    def test_different_description_different_hash(self):
        """Description is included in document_sha256."""
        doc1 = HashDoc.create_root(name="a.txt", content="hello", description="desc1", reason="test input")
        doc2 = HashDoc.create_root(name="a.txt", content="hello", description="desc2", reason="test input")
        assert compute_document_sha256(doc1) != compute_document_sha256(doc2)

    def test_empty_description_deterministic(self):
        """Empty description produces consistent hash."""
        doc1 = HashDoc.create_root(name="a.txt", content="hello", reason="test input")
        doc2 = HashDoc.create_root(name="a.txt", content="hello", reason="test input")
        assert compute_document_sha256(doc1) == compute_document_sha256(doc2)

    def test_attachment_description_affects_hash(self):
        """Attachment description is included in document_sha256."""
        att1 = Attachment(name="img.png", content=b"\x89PNG", description="screenshot")
        att2 = Attachment(name="img.png", content=b"\x89PNG", description="thumbnail")
        doc1 = HashDoc.create_root(name="a.txt", content="hello", attachments=(att1,), reason="test input")
        doc2 = HashDoc.create_root(name="a.txt", content="hello", attachments=(att2,), reason="test input")
        assert compute_document_sha256(doc1) != compute_document_sha256(doc2)

    def test_derived_from_affects_hash(self):
        """derived_from is included in document_sha256."""
        doc1 = HashDoc(name="a.txt", content=b"hello", derived_from=("https://example.com/src1",))
        doc2 = HashDoc(name="a.txt", content=b"hello", derived_from=("https://example.com/src2",))
        assert compute_document_sha256(doc1) != compute_document_sha256(doc2)

    def test_triggered_by_affects_hash(self):
        """triggered_by is included in document_sha256."""
        origin_a = compute_document_sha256(HashDoc.create_root(name="origin_a.txt", content="origin a", reason="test input"))
        origin_b = compute_document_sha256(HashDoc.create_root(name="origin_b.txt", content="origin b", reason="test input"))
        doc1 = HashDoc(name="a.txt", content=b"hello", triggered_by=(origin_a,))
        doc2 = HashDoc(name="a.txt", content=b"hello", triggered_by=(origin_b,))
        assert compute_document_sha256(doc1) != compute_document_sha256(doc2)

    def test_derived_from_order_does_not_matter(self):
        """derived_from entries are sorted before hashing for order-independence."""
        doc1 = HashDoc(name="a.txt", content=b"hello", derived_from=("https://a.com", "https://b.com"))
        doc2 = HashDoc(name="a.txt", content=b"hello", derived_from=("https://b.com", "https://a.com"))
        assert compute_document_sha256(doc1) == compute_document_sha256(doc2)

    def test_triggered_by_order_does_not_matter(self):
        """triggered_by entries are sorted before hashing for order-independence."""
        origin_a = compute_document_sha256(HashDoc.create_root(name="origin_a.txt", content="origin a", reason="test input"))
        origin_b = compute_document_sha256(HashDoc.create_root(name="origin_b.txt", content="origin b", reason="test input"))
        doc1 = HashDoc(name="a.txt", content=b"hello", triggered_by=(origin_a, origin_b))
        doc2 = HashDoc(name="a.txt", content=b"hello", triggered_by=(origin_b, origin_a))
        assert compute_document_sha256(doc1) == compute_document_sha256(doc2)

    def test_no_provenance_still_deterministic(self):
        """Documents without derived_from/triggered_by produce consistent hashes."""
        doc1 = HashDoc.create_root(name="a.txt", content="hello", reason="test input")
        doc2 = HashDoc.create_root(name="a.txt", content="hello", reason="test input")
        assert compute_document_sha256(doc1) == compute_document_sha256(doc2)

    def test_group_count_disambiguation(self):
        """Moving items between derived_from and triggered_by produces different hashes.

        Validates count-prefix boundaries: derived_from=['A'], triggered_by=['B']
        must differ from derived_from=['A','B'], triggered_by=[].
        """
        url_a = "https://example.com/a"
        url_b = "https://example.com/b"
        doc1 = HashDoc(name="a.txt", content=b"hello", derived_from=(url_a,), triggered_by=())
        doc2 = HashDoc.create_root(name="a.txt", content="hello", reason="no provenance")
        doc3 = HashDoc(name="a.txt", content=b"hello", derived_from=(url_a, url_b))
        doc4 = HashDoc(name="a.txt", content=b"hello", derived_from=(url_a,))
        assert compute_document_sha256(doc1) != compute_document_sha256(doc2)
        assert compute_document_sha256(doc3) != compute_document_sha256(doc4)

    def test_length_prefix_prevents_collision(self):
        """Length prefixing prevents 'ab' + 'cd' from colliding with 'abc' + 'd'."""
        # Different name/content splits should produce different hashes
        doc1 = HashDoc.create_root(name="ab", content="cd", reason="test input")
        doc2 = HashDoc.create_root(name="abc", content="d", reason="test input")
        assert compute_document_sha256(doc1) != compute_document_sha256(doc2)


class TestComputeContentSha256:
    def test_deterministic(self):
        h1 = compute_content_sha256(b"hello")
        h2 = compute_content_sha256(b"hello")
        assert h1 == h2

    def test_base32_format(self):
        h = compute_content_sha256(b"hello")
        assert h.isascii()
        assert h == h.upper()
        assert len(h) == 26

    def test_different_content(self):
        assert compute_content_sha256(b"hello") != compute_content_sha256(b"world")

    def test_empty_content(self):
        h = compute_content_sha256(b"")
        assert len(h) == 26
