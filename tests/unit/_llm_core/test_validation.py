"""Unit tests for ai_pipeline_core._llm_core._validation."""

from io import BytesIO

from PIL import Image

from ai_pipeline_core._llm_core._validation import (
    _MAX_IMAGE_PIXELS,
    validate_image_content,
    validate_pdf_content,
)
from ai_pipeline_core.llm._conversation_messages import validate_text


def _encode_png(width: int, height: int) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), color=(127, 127, 127)).save(buffer, format="PNG")
    return buffer.getvalue()


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

    def test_long_screenshot_under_total_pixel_cap_accepted(self):
        """A 1280x16383 mobile screenshot (~21M px) fits well within the 100M total-pixel cap."""
        png = _encode_png(1280, 16383)
        assert validate_image_content(png) is None

    def test_very_wide_image_under_total_pixel_cap_accepted(self):
        """A 16383x1280 image (mirror of the screenshot case) is accepted on the wide axis too."""
        png = _encode_png(16383, 1280)
        assert validate_image_content(png) is None

    def test_total_pixel_cap_rejected(self):
        """An image whose total pixels exceed the cap is rejected with a total-pixel message."""
        side = int(_MAX_IMAGE_PIXELS**0.5) + 50  # ~10,050 — total slightly exceeds 100M
        png = _encode_png(side, side)
        result = validate_image_content(png)
        assert result is not None
        assert "total pixels" in result
        assert f"{side}x{side}" in result


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
