"""Tests for Document approximate_tokens_count property."""

import pytest

from ai_pipeline_core.documents.attachment import Attachment
from tests.support.helpers import ConcreteDocument


@pytest.fixture(autouse=True)
def _suppress_registration():
    return


class TestDocumentApproximateTokensCount:
    """Test Document approximate_tokens_count property."""

    def test_text_document_tokens(self):
        """Test token count for text document."""
        doc = ConcreteDocument(name="test.txt", content=b"Hello world")
        count = doc.approximate_tokens_count
        assert count > 0
        assert isinstance(count, int)
        assert count < 10  # Should be very few tokens

    def test_long_text_document(self):
        """Test token count for long text document."""
        long_text = b"This is a much longer document " * 100
        doc = ConcreteDocument(name="long.txt", content=long_text)
        count = doc.approximate_tokens_count
        assert count > 100  # Should have many tokens

    def test_empty_text_document(self):
        """Test token count for empty text document."""
        doc = ConcreteDocument(name="empty.txt", content=b"")
        count = doc.approximate_tokens_count
        # Empty content is detected as text/plain, so returns 0 tokens
        assert count == 0

    def test_unicode_text_document(self):
        """Test token count with unicode content."""
        doc = ConcreteDocument(name="unicode.txt", content="Hello 世界 🌍".encode())
        count = doc.approximate_tokens_count
        assert count > 0
        assert isinstance(count, int)

    def test_non_text_document_fixed_estimate(self):
        """Test that image documents return fixed 1080 estimate."""
        # Binary PNG header
        doc = ConcreteDocument(name="image.png", content=b"\x89PNG\r\n\x1a\n")
        count = doc.approximate_tokens_count
        assert count == 1080  # Fixed estimate for image documents

    def test_binary_document_fixed_estimate(self):
        """Test that binary documents return fixed 1024 estimate."""
        doc = ConcreteDocument(name="data.bin", content=b"\x00\x01\x02\x03\x04")
        count = doc.approximate_tokens_count
        assert count == 1024  # Fixed estimate

    def test_consistency(self):
        """Test that token count is consistent for same document."""
        doc = ConcreteDocument(name="test.txt", content=b"Consistent content")
        count1 = doc.approximate_tokens_count
        count2 = doc.approximate_tokens_count
        count3 = doc.approximate_tokens_count
        assert count1 == count2 == count3

    def test_json_document_tokens(self):
        """Test token count for JSON document."""
        json_content = b'{"name": "test", "value": 123, "nested": {"key": "value"}}'
        doc = ConcreteDocument(name="data.json", content=json_content)
        count = doc.approximate_tokens_count
        assert count > 0

    def test_code_document_tokens(self):
        """Test token count for code document."""
        code = b"""
def hello_world():
    print("Hello, World!")
    return True
"""
        doc = ConcreteDocument(name="code.py", content=code)
        count = doc.approximate_tokens_count
        assert count > 0

    def test_multiline_document(self):
        """Test token count for multiline document."""
        multiline = b"""Line 1
Line 2
Line 3
Line 4
"""
        doc = ConcreteDocument(name="lines.txt", content=multiline)
        count = doc.approximate_tokens_count
        assert count > 0

    def test_document_with_description_tokens(self):
        """Test token count only counts content, not description."""
        doc1 = ConcreteDocument(
            name="test.txt",
            content=b"Content",
        )
        doc2 = ConcreteDocument(
            name="test.txt",
            content=b"Content",
            description="This is a description",
        )
        # Both should have same token count (description not included)
        assert doc1.approximate_tokens_count == doc2.approximate_tokens_count

    def test_special_characters_tokens(self):
        """Test token count with special characters."""
        doc = ConcreteDocument(name="special.txt", content=b"Hello! @#$% ^&*() <>?")
        count = doc.approximate_tokens_count
        assert count > 0

    def test_numbers_tokens(self):
        """Test token count with numbers."""
        doc = ConcreteDocument(name="numbers.txt", content=b"The year 2024 has 365 days")
        count = doc.approximate_tokens_count
        assert count > 0

    def test_very_large_document(self):
        """Test token count for very large document."""
        large_content = b"Word " * 10000
        doc = ConcreteDocument(name="large.txt", content=large_content)
        count = doc.approximate_tokens_count
        assert count > 5000  # Should have many tokens

    def test_markdown_document(self):
        """Test token count for markdown document."""
        markdown = b"""# Header

This is **bold** and this is *italic*.

- List item 1
- List item 2
"""
        doc = ConcreteDocument(name="doc.md", content=markdown)
        count = doc.approximate_tokens_count
        assert count > 0


class TestAttachmentTokensCounting:
    """Test approximate_tokens_count includes attachment tokens."""

    def test_empty_attachments_unchanged(self):
        """Token count is unchanged when attachments tuple is empty."""
        doc = ConcreteDocument(name="test.txt", content=b"Hello world")
        base_count = doc.approximate_tokens_count
        assert base_count > 0
        # Same as a doc without attachments
        doc2 = ConcreteDocument(name="test.txt", content=b"Hello world", attachments=())
        assert doc2.approximate_tokens_count == base_count

    def test_image_attachment_adds_1080(self):
        """Image attachment adds 1080 tokens."""
        doc_no_att = ConcreteDocument(name="test.txt", content=b"Hello")
        base = doc_no_att.approximate_tokens_count
        doc_with_att = ConcreteDocument(
            name="test.txt",
            content=b"Hello",
            attachments=(Attachment(name="img.png", content=b"\x89PNG\r\n\x1a\n"),),
        )
        assert doc_with_att.approximate_tokens_count == base + 1080

    def test_text_attachment_uses_tiktoken(self):
        """Text attachment uses tiktoken for counting."""
        import tiktoken

        att_text = "Some attachment text content"
        expected_att_tokens = len(tiktoken.get_encoding("cl100k_base").encode(att_text))
        doc = ConcreteDocument(
            name="test.txt",
            content=b"Main",
            attachments=(Attachment(name="note.txt", content=att_text.encode("utf-8")),),
        )
        base = ConcreteDocument(name="test.txt", content=b"Main").approximate_tokens_count
        assert doc.approximate_tokens_count == base + expected_att_tokens

    def test_pdf_attachment_adds_1024(self):
        """PDF attachment adds 1024 tokens."""
        pdf_content = b"%PDF-1.4\n%\xd3\xeb\xe9\xe1\n1 0 obj\n<</Type/Catalog>>\nendobj"
        doc_no_att = ConcreteDocument(name="test.txt", content=b"Main")
        base = doc_no_att.approximate_tokens_count
        doc = ConcreteDocument(
            name="test.txt",
            content=b"Main",
            attachments=(Attachment(name="doc.pdf", content=pdf_content),),
        )
        assert doc.approximate_tokens_count == base + 1024

    def test_unknown_binary_attachment_adds_1024(self):
        """Unknown binary format attachment adds 1024 tokens."""
        doc_no_att = ConcreteDocument(name="test.txt", content=b"Main")
        base = doc_no_att.approximate_tokens_count
        doc = ConcreteDocument(
            name="test.txt",
            content=b"Main",
            attachments=(Attachment(name="data.bin", content=b"\x00\x01\x02\x03"),),
        )
        assert doc.approximate_tokens_count == base + 1024

    def test_mixed_attachments(self):
        """Multiple mixed attachments accumulate correctly."""
        import tiktoken

        att_text = "text content"
        text_tokens = len(tiktoken.get_encoding("cl100k_base").encode(att_text))
        doc = ConcreteDocument(
            name="test.txt",
            content=b"Main",
            attachments=(
                Attachment(name="img.png", content=b"\x89PNG\r\n\x1a\n"),
                Attachment(name="note.txt", content=att_text.encode("utf-8")),
                Attachment(name="data.bin", content=b"\x00\x01"),
            ),
        )
        base = ConcreteDocument(name="test.txt", content=b"Main").approximate_tokens_count
        assert doc.approximate_tokens_count == base + 1080 + text_tokens + 1024
