"""Unit tests for ai_pipeline_core._llm_core._validation."""

from ai_pipeline_core._llm_core._validation import (
    validate_image_content,
    validate_pdf_content,
)
from ai_pipeline_core.llm._conversation_messages import validate_text


class TestValidateImageContent:
    def test_empty_data(self):
        result = validate_image_content(b"")
        assert result is not None
        assert "empty" in result

    def test_valid_image(self):
        # Minimal valid 1x1 GIF
        gif = (
            b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00"
            b"!\xf9\x04\x00\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
        )
        assert validate_image_content(gif) is None

    def test_invalid_image(self):
        result = validate_image_content(b"not an image")
        assert result is not None
        assert "invalid" in result


class TestValidatePdfContent:
    def test_empty_data(self):
        result = validate_pdf_content(b"")
        assert result is not None
        assert "empty" in result

    def test_invalid_header(self):
        result = validate_pdf_content(b"not a pdf")
        assert result is not None
        assert "header" in result

    def test_corrupted_pdf(self):
        result = validate_pdf_content(b"%PDF-1.4 corrupted content")
        assert result is not None


class TestValidateText:
    """Tests for validate_text helper that now lives in llm/_conversation_messages.py."""

    def test_empty_data(self):
        result = validate_text(b"", "test.txt")
        assert result is not None
        assert "empty" in result

    def test_null_bytes(self):
        result = validate_text(b"hello\x00world", "test.txt")
        assert result is not None
        assert "binary" in result or "null" in result

    def test_invalid_utf8(self):
        result = validate_text(b"\xff\xfe invalid", "test.txt")
        assert result is not None
        assert "UTF-8" in result

    def test_valid_text(self):
        assert validate_text(b"Hello world", "test.txt") is None
