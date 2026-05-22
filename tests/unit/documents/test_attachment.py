"""Tests for the Attachment Pydantic model."""

import base64

import pytest
from pydantic import ValidationError

from ai_pipeline_core.documents.attachment import Attachment
from ai_pipeline_core.exceptions import DocumentNameError

# --- Binary content fixtures ---

PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
JPEG_HEADER = b"\xff\xd8\xff\xe0" + b"\x00" * 50
PDF_HEADER = b"%PDF-1.4\n%\xd3\xeb\xe9\xe1\n1 0 obj\n<</Type/Catalog>>\nendobj"


class TestAttachmentConstruction:
    """Test Attachment model construction."""

    def test_create_with_text_content(self):
        att = Attachment(name="notes.txt", content=b"Hello world")
        assert att.name == "notes.txt"
        assert att.content == b"Hello world"
        assert att.description == ""

    def test_create_with_binary_content(self):
        att = Attachment(name="image.png", content=PNG_HEADER)
        assert att.name == "image.png"
        assert att.content == PNG_HEADER
        assert att.description == ""

    def test_create_with_description(self):
        att = Attachment(name="screenshot.jpg", content=JPEG_HEADER, description="Homepage screenshot")
        assert att.description == "Homepage screenshot"

    def test_create_without_description(self):
        att = Attachment(name="file.txt", content=b"data")
        assert att.description == ""


class TestAttachmentNameValidation:
    """Test name field validation."""

    def test_rejects_empty_string(self):
        with pytest.raises(DocumentNameError):
            Attachment(name="", content=b"data")

    def test_rejects_forward_slash(self):
        with pytest.raises(DocumentNameError, match="path traversal"):
            Attachment(name="path/file.txt", content=b"data")

    def test_rejects_backslash(self):
        with pytest.raises(DocumentNameError, match="path traversal"):
            Attachment(name="path\\file.txt", content=b"data")

    def test_rejects_description_md_suffix(self):
        with pytest.raises(DocumentNameError, match=r".description.md"):
            Attachment(name="test.description.md", content=b"data")

    def test_rejects_sources_json_suffix(self):
        with pytest.raises(DocumentNameError, match=r".sources.json"):
            Attachment(name="test.sources.json", content=b"data")

    def test_rejects_double_dot(self):
        with pytest.raises(DocumentNameError, match="path traversal"):
            Attachment(name="..file.txt", content=b"data")

    def test_rejects_leading_whitespace(self):
        with pytest.raises(DocumentNameError):
            Attachment(name=" file.txt", content=b"data")

    def test_rejects_trailing_whitespace(self):
        with pytest.raises(DocumentNameError):
            Attachment(name="file.txt ", content=b"data")

    def test_accepts_valid_names(self):
        valid_names = ["file.txt", "screenshot.png", "report-v2.pdf", "data_2024.json", "a"]
        for name in valid_names:
            att = Attachment(name=name, content=b"data")
            assert att.name == name


class TestAttachmentSerializeContent:
    """Test serialize_content field serializer."""

    def test_text_content_returns_plain_string(self):
        att = Attachment(name="notes.txt", content=b"Hello world")
        dumped = att.model_dump()
        assert dumped["content"] == "Hello world"

    def test_binary_content_returns_data_uri(self):
        att = Attachment(name="image.png", content=PNG_HEADER)
        dumped = att.model_dump()
        expected = base64.b64encode(PNG_HEADER).decode("ascii")
        assert dumped["content"] == f"data:image/png;base64,{expected}"

    def test_unicode_text_returns_plain_string(self):
        content = "Hello 世界".encode()
        att = Attachment(name="unicode.txt", content=content)
        dumped = att.model_dump()
        assert dumped["content"] == "Hello 世界"


class TestAttachmentProperties:
    """Test Attachment properties."""

    def test_is_image_true_for_png(self):
        att = Attachment(name="img.png", content=PNG_HEADER)
        assert att.is_image is True

    def test_is_image_true_for_jpeg(self):
        att = Attachment(name="photo.jpg", content=JPEG_HEADER)
        assert att.is_image is True

    def test_is_image_false_for_text(self):
        att = Attachment(name="notes.txt", content=b"Hello")
        assert att.is_image is False

    def test_is_pdf_true_for_pdf(self):
        att = Attachment(name="doc.pdf", content=PDF_HEADER)
        assert att.is_pdf is True

    def test_is_pdf_false_for_text(self):
        att = Attachment(name="notes.txt", content=b"Hello")
        assert att.is_pdf is False

    def test_is_text_true_for_text(self):
        att = Attachment(name="notes.txt", content=b"Hello")
        assert att.is_text is True

    def test_is_text_false_for_binary(self):
        att = Attachment(name="img.png", content=PNG_HEADER)
        assert att.is_text is False

    def test_mime_type_text(self):
        att = Attachment(name="notes.txt", content=b"Hello")
        assert "text" in att.mime_type

    def test_mime_type_png(self):
        att = Attachment(name="img.png", content=PNG_HEADER)
        assert "image" in att.mime_type

    def test_mime_type_pdf(self):
        att = Attachment(name="doc.pdf", content=PDF_HEADER)
        assert "pdf" in att.mime_type

    def test_size_returns_correct_byte_count(self):
        content = b"Hello world"
        att = Attachment(name="test.txt", content=content)
        assert att.size == len(content)

    def test_size_for_binary(self):
        att = Attachment(name="img.png", content=PNG_HEADER)
        assert att.size == len(PNG_HEADER)

    def test_text_returns_decoded_string(self):
        att = Attachment(name="notes.txt", content=b"Hello world")
        assert att.text == "Hello world"

    def test_text_raises_for_binary_content(self):
        att = Attachment(name="img.png", content=PNG_HEADER)
        with pytest.raises(ValueError, match="not text"):
            att.text


class TestAttachmentImmutability:
    """Test that Attachment is frozen."""

    def test_cannot_set_name(self):
        att = Attachment(name="test.txt", content=b"data")
        with pytest.raises(ValidationError):
            att.name = "other.txt"  # type: ignore[misc]

    def test_cannot_set_content(self):
        att = Attachment(name="test.txt", content=b"data")
        with pytest.raises(ValidationError):
            att.content = b"other"  # type: ignore[misc]

    def test_cannot_set_description(self):
        att = Attachment(name="test.txt", content=b"data", description="desc")
        with pytest.raises(ValidationError):
            att.description = "new"  # type: ignore[misc]


class TestAttachmentExtraFields:
    """Test that extra fields are rejected."""

    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError, match="extra"):
            Attachment(name="test.txt", content=b"data", extra_field="bad")  # type: ignore[call-arg]
