"""Tests for Document behavior with attachments (SHA256, size, tokens, validation, copy)."""

import pytest

from ai_pipeline_core.documents import Document
from ai_pipeline_core.documents.attachment import Attachment
from ai_pipeline_core.exceptions import DocumentSizeError


class SampleFlowDoc(Document):
    """Concrete Document for attachment tests."""


class SmallLimitDoc(Document):
    """Document with small size limit for validation tests."""

    MAX_CONTENT_SIZE = 50


@pytest.fixture(autouse=True)
def _suppress_registration():
    return


# --- Binary fixtures ---

PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
PDF_HEADER = b"%PDF-1.4\n%\xd3\xeb\xe9\xe1\n1 0 obj\n<</Type/Catalog>>\nendobj"


class TestSHA256WithAttachments:
    """Test that SHA256 incorporates attachments correctly."""

    def test_with_attachments_differs_from_without(self):
        doc_plain = SampleFlowDoc(name="test.txt", content=b"Hello")
        doc_with_att = SampleFlowDoc(
            name="test.txt",
            content=b"Hello",
            attachments=(Attachment(name="a.txt", content=b"extra"),),
        )
        assert doc_plain.sha256 != doc_with_att.sha256

    def test_same_content_as_attachment_changes_hash(self):
        """Adding the same content string as an attachment still changes the hash."""
        doc_plain = SampleFlowDoc(name="test.txt", content=b"Hello")
        doc_with_att = SampleFlowDoc(
            name="test.txt",
            content=b"Hello",
            attachments=(Attachment(name="dup.txt", content=b"Hello"),),
        )
        assert doc_plain.sha256 != doc_with_att.sha256

    def test_changing_attachment_name_changes_hash(self):
        att_a = Attachment(name="a.txt", content=b"data")
        att_b = Attachment(name="b.txt", content=b"data")
        doc_a = SampleFlowDoc(name="test.txt", content=b"Hello", attachments=(att_a,))
        doc_b = SampleFlowDoc(name="test.txt", content=b"Hello", attachments=(att_b,))
        assert doc_a.sha256 != doc_b.sha256

    def test_changing_attachment_content_changes_hash(self):
        att_a = Attachment(name="a.txt", content=b"alpha")
        att_b = Attachment(name="a.txt", content=b"beta")
        doc_a = SampleFlowDoc(name="test.txt", content=b"Hello", attachments=(att_a,))
        doc_b = SampleFlowDoc(name="test.txt", content=b"Hello", attachments=(att_b,))
        assert doc_a.sha256 != doc_b.sha256

    def test_attachment_order_does_not_affect_hash(self):
        """Attachments are sorted by name before hashing, so order doesn't matter."""
        att_x = Attachment(name="x.txt", content=b"xxx")
        att_y = Attachment(name="y.txt", content=b"yyy")
        doc_xy = SampleFlowDoc(name="test.txt", content=b"Hello", attachments=(att_x, att_y))
        doc_yx = SampleFlowDoc(name="test.txt", content=b"Hello", attachments=(att_y, att_x))
        assert doc_xy.sha256 == doc_yx.sha256

    def test_identical_attachments_produce_same_hash(self):
        att = Attachment(name="a.txt", content=b"data")
        doc1 = SampleFlowDoc(name="test.txt", content=b"Hello", attachments=(att,))
        doc2 = SampleFlowDoc(name="test.txt", content=b"Hello", attachments=(att,))
        assert doc1.sha256 == doc2.sha256


class TestSizeWithAttachments:
    """Test size property includes attachment content sizes."""

    def test_size_includes_attachment_sizes(self):
        doc = SampleFlowDoc(
            name="test.txt",
            content=b"Hello",  # 5 bytes
            attachments=(
                Attachment(name="a.txt", content=b"abc"),  # 3 bytes
                Attachment(name="b.bin", content=b"\x00\x01"),  # 2 bytes
            ),
        )
        assert doc.size == 5 + 3 + 2

    def test_empty_attachments_does_not_affect_size(self):
        doc = SampleFlowDoc(name="test.txt", content=b"Hello")
        assert doc.size == 5

        doc_empty = SampleFlowDoc(name="test.txt", content=b"Hello", attachments=())
        assert doc_empty.size == 5


class TestTokenCountWithAttachments:
    """Test approximate_tokens_count includes attachment tokens."""

    def test_text_attachment_contributes_tokens(self):
        import tiktoken

        att_text = "Some attachment text"
        expected_att_tokens = len(tiktoken.get_encoding("cl100k_base").encode(att_text))
        doc = SampleFlowDoc(
            name="test.txt",
            content=b"Main",
            attachments=(Attachment(name="note.txt", content=att_text.encode("utf-8")),),
        )
        base = SampleFlowDoc(name="test.txt", content=b"Main").approximate_tokens_count
        assert doc.approximate_tokens_count == base + expected_att_tokens

    def test_image_attachment_contributes_fixed_1080(self):
        doc = SampleFlowDoc(
            name="test.txt",
            content=b"Main",
            attachments=(Attachment(name="img.png", content=PNG_HEADER),),
        )
        base = SampleFlowDoc(name="test.txt", content=b"Main").approximate_tokens_count
        assert doc.approximate_tokens_count == base + 1080

    def test_pdf_attachment_contributes_fixed_1024(self):
        doc = SampleFlowDoc(
            name="test.txt",
            content=b"Main",
            attachments=(Attachment(name="doc.pdf", content=PDF_HEADER),),
        )
        base = SampleFlowDoc(name="test.txt", content=b"Main").approximate_tokens_count
        assert doc.approximate_tokens_count == base + 1024


class TestTotalSizeValidation:
    """Test validate_total_size model validator with attachments."""

    def test_content_plus_attachments_exceeding_limit_rejected(self):
        with pytest.raises(DocumentSizeError, match="including attachments"):
            SmallLimitDoc(
                name="test.txt",
                content=b"A" * 30,  # 30 bytes
                attachments=(Attachment(name="a.txt", content=b"B" * 25),),  # total 55 > 50
            )

    def test_content_plus_attachments_within_limit_accepted(self):
        doc = SmallLimitDoc(
            name="test.txt",
            content=b"A" * 20,  # 20 bytes
            attachments=(Attachment(name="a.txt", content=b"B" * 20),),  # 20 bytes => total 40 < 50
        )
        assert doc.size == 40


class TestModelCopyWithAttachments:
    """model_copy is blocked on Document — verify it raises TypeError."""

    def test_model_copy_replaces_attachments(self):
        doc = SampleFlowDoc(name="test.txt", content=b"Hello")
        with pytest.raises(TypeError, match=r"model_copy.*not supported"):
            doc.model_copy(update={"attachments": ()})

    def test_model_copy_clears_attachments(self):
        doc = SampleFlowDoc(name="test.txt", content=b"Hello")
        with pytest.raises(TypeError, match=r"model_copy.*not supported"):
            doc.model_copy(update={"attachments": ()})

    def test_model_copy_preserves_attachments_when_not_updated(self):
        doc = SampleFlowDoc(name="test.txt", content=b"Hello")
        with pytest.raises(TypeError, match=r"model_copy.*not supported"):
            doc.model_copy(update={"description": "new desc"})


class TestEmptyTupleAttachments:
    """Test that attachments=() behaves same as default."""

    def test_empty_tuple_same_as_default(self):
        doc_default = SampleFlowDoc(name="test.txt", content=b"Hello")
        doc_explicit = SampleFlowDoc(name="test.txt", content=b"Hello", attachments=())
        assert doc_default.attachments == doc_explicit.attachments
        assert doc_default.sha256 == doc_explicit.sha256
        assert doc_default.size == doc_explicit.size
        assert doc_default.approximate_tokens_count == doc_explicit.approximate_tokens_count
