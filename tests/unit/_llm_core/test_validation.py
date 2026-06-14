"""Unit tests for ai_pipeline_core._llm_core._validation."""

from io import BytesIO

import pytest
from PIL import Image

from ai_pipeline_core._llm_core import _validation
from ai_pipeline_core._llm_core._validation import (
    _MAX_IMAGE_PIXELS,
    validate_image_content,
    validate_pdf_content,
)
from ai_pipeline_core.llm._request_messages import validate_text


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

    def test_total_pixel_cap_rejected(self, monkeypatch: pytest.MonkeyPatch):
        """An image whose total pixels exceed the cap is rejected with a total-pixel message.

        The cap is monkeypatched down to a tiny value so the test runs in milliseconds
        and never allocates a multi-gigabyte raw buffer in CI. The 100M production
        default is preserved at import time and exercised end-to-end by the benchmark
        matrix.
        """
        monkeypatch.setattr(_validation, "_MAX_IMAGE_PIXELS", 100)
        png = _encode_png(11, 11)  # 121 pixels > 100
        result = validate_image_content(png)
        assert result is not None
        assert "total pixels" in result
        assert "11x11" in result

    def test_production_pixel_cap_constant_unchanged(self):
        """Guard rail: the production cap stays at 100M unless explicitly raised."""
        assert _MAX_IMAGE_PIXELS == 100_000_000


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

    def test_lazy_parse_failure_returns_error_string(self):
        """PdfReader constructs successfully but ``len(reader.pages)`` raises.

        Regression guard for the v0.23.5 CI failure: a hand-crafted PDF with
        wrong xref byte offsets AND a content stream object passes the
        ``PdfReader(BytesIO(...))`` step (pypdf logs warnings and recovers),
        then raises ``ValueError`` during page resolution when the stream
        reader misparses ``<<`` as a hex-string opener. The validator must
        catch the lazy-parse exception and return the documented error
        string, not propagate.
        """
        # Byte offsets in this xref are deliberately wrong; the /Length 44
        # claim for the 36-byte stream is what triggers the hex-misparse path
        # during len(reader.pages).
        broken_pdf = (
            b"%PDF-1.4\n"
            b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 100 100]/Contents 4 0 R>>endobj\n"
            b"4 0 obj<</Length 44>>stream\nBT /F1 24 Tf 10 50 Td (Hello) Tj ET\nendstream\nendobj\n"
            b"xref\n0 5\n0000000000 65535 f \n0000000009 00000 n \n0000000052 00000 n \n"
            b"0000000093 00000 n \n0000000172 00000 n \n"
            b"trailer<</Size 5/Root 1 0 R>>\nstartxref\n260\n%%EOF\n"
        )
        result = validate_pdf_content(broken_pdf)
        assert result is not None
        assert "corrupted PDF" in result


class TestValidateText:
    """Tests for validate_text helper that now lives in llm/_request_messages.py."""

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
