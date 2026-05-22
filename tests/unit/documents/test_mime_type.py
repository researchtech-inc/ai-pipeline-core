"""Tests for MIME type utility functions."""

from ai_pipeline_core.documents._mime_type import (
    detect_mime_type,
    is_image_mime_type,
    is_pdf_mime_type,
    is_text_mime_type,
    is_yaml_mime_type,
)


class TestMimeTypeDetection:
    """Test MIME type detection functions."""

    def test_detect_text_files(self):
        """Test detection of text file MIME types."""
        assert detect_mime_type(b"Hello, world!", "test.txt") == "text/plain"
        assert detect_mime_type(b"# Header", "README.md") == "text/markdown"
        assert detect_mime_type(b"print('hello')", "script.py") == "text/x-python"
        assert detect_mime_type(b"body { margin: 0; }", "style.css") == "text/css"
        assert detect_mime_type(b"<html></html>", "index.html") == "text/html"

    def test_detect_json_yaml_files(self):
        """Test detection of JSON and YAML file MIME types."""
        assert detect_mime_type(b'{"key": "value"}', "data.json") == "application/json"
        assert detect_mime_type(b"key: value\n", "config.yaml") == "application/yaml"
        assert detect_mime_type(b"key: value\n", "config.yml") == "application/yaml"

    def test_detect_image_files(self):
        """Test detection of image file MIME types."""
        # PNG header
        png_data = b"\x89PNG\r\n\x1a\n"
        assert detect_mime_type(png_data, "image.png") == "image/png"

        # JPEG header (need at least 4 bytes for content detection)
        jpeg_data = b"\xff\xd8\xff\xe0"
        assert detect_mime_type(jpeg_data, "photo.jpg") == "image/jpeg"
        assert detect_mime_type(jpeg_data, "photo.jpeg") == "image/jpeg"

    def test_detect_pdf_files(self):
        """Test detection of PDF file MIME types."""
        pdf_data = b"%PDF-1.4"
        assert detect_mime_type(pdf_data, "document.pdf") == "application/pdf"

    def test_detect_binary_files(self):
        """Test detection of binary file MIME types."""
        assert detect_mime_type(b"\x00\x01\x02\x03", "data.bin") == "application/octet-stream"
        assert detect_mime_type(b"\x00\x01\x02\x03", "unknown") == "application/octet-stream"


class TestMimeTypeCheckers:
    """Test MIME type checker functions."""

    def test_is_text_mime_type(self):
        """Test is_text_mime_type function."""
        # Text types
        assert is_text_mime_type("text/plain") is True
        assert is_text_mime_type("text/html") is True
        assert is_text_mime_type("text/css") is True
        assert is_text_mime_type("text/javascript") is True
        assert is_text_mime_type("text/markdown") is True
        assert is_text_mime_type("text/x-python") is True

        # Application text types
        assert is_text_mime_type("application/json") is True
        assert is_text_mime_type("application/xml") is True
        assert is_text_mime_type("application/yaml") is True
        assert is_text_mime_type("application/javascript") is True

        # Non-text types
        assert is_text_mime_type("image/png") is False
        assert is_text_mime_type("image/jpeg") is False
        assert is_text_mime_type("application/pdf") is False
        assert is_text_mime_type("application/octet-stream") is False
        assert is_text_mime_type("video/mp4") is False

    def test_is_yaml_mime_type(self):
        """Test is_yaml_mime_type function."""
        # YAML types
        assert is_yaml_mime_type("application/yaml") is True
        assert is_yaml_mime_type("application/x-yaml") is True

        # Non-YAML types
        assert is_yaml_mime_type("text/plain") is False
        assert is_yaml_mime_type("application/json") is False
        assert is_yaml_mime_type("text/yaml") is False  # Not standard YAML MIME type

    def test_is_image_mime_type(self):
        """Test is_image_mime_type function."""
        # Image types
        assert is_image_mime_type("image/png") is True
        assert is_image_mime_type("image/jpeg") is True
        assert is_image_mime_type("image/gif") is True
        assert is_image_mime_type("image/bmp") is True
        assert is_image_mime_type("image/svg+xml") is True
        assert is_image_mime_type("image/webp") is True

        # Non-image types
        assert is_image_mime_type("text/plain") is False
        assert is_image_mime_type("application/pdf") is False
        assert is_image_mime_type("video/mp4") is False

    def test_is_pdf_mime_type(self):
        """Test is_pdf_mime_type function."""
        # PDF type
        assert is_pdf_mime_type("application/pdf") is True

        # Non-PDF types
        assert is_pdf_mime_type("text/plain") is False
        assert is_pdf_mime_type("image/png") is False
        assert is_pdf_mime_type("application/octet-stream") is False


class TestMimeTypeEdgeCases:
    """Test edge cases and special scenarios."""

    def test_empty_content(self):
        """Test MIME type detection with empty content."""
        # Empty content always returns text/plain regardless of extension
        assert detect_mime_type(b"", "empty.txt") == "text/plain"
        assert detect_mime_type(b"", "empty.json") == "text/plain"
        assert detect_mime_type(b"", "empty.yaml") == "text/plain"

    def test_unknown_extensions(self):
        """Test MIME type detection with unknown extensions."""
        # Unknown extensions with text content
        assert detect_mime_type(b"text content", "file.xyz") == "text/plain"

        # Unknown extensions with binary content - magic detects as octet-stream
        assert detect_mime_type(b"\x00\x01\x02", "file.xyz") == "application/octet-stream"
        # Larger binary content should also be detected as octet-stream
        assert detect_mime_type(b"\x00\x01\x02\x03\x04", "file.xyz") == "application/octet-stream"

    def test_no_extension(self):
        """Test MIME type detection without file extension."""
        # Text content without extension
        assert detect_mime_type(b"Documentation", "README") == "text/plain"

        # Binary content without extension - magic detects as octet-stream
        assert detect_mime_type(b"\x00\x01\x02", "data") == "application/octet-stream"
        # Larger binary content
        assert detect_mime_type(b"\x00\x01\x02\x03\x04", "data") == "application/octet-stream"

    def test_case_insensitive_extensions(self):
        """Test that MIME type detection handles case-insensitive extensions."""
        assert detect_mime_type(b"content", "FILE.TXT") == "text/plain"
        assert detect_mime_type(b'{"a":1}', "DATA.JSON") == "application/json"
        assert detect_mime_type(b"key: value", "CONFIG.YAML") == "application/yaml"
        assert detect_mime_type(b"key: value", "CONFIG.YML") == "application/yaml"

    def test_mime_type_consistency(self):
        """Test that MIME type checkers are consistent with detection."""
        # JSON
        json_mime = detect_mime_type(b'{"key": "value"}', "data.json")
        assert json_mime == "application/json"
        assert is_text_mime_type(json_mime) is True

        # YAML
        yaml_mime = detect_mime_type(b"key: value", "config.yaml")
        assert is_yaml_mime_type(yaml_mime) is True
        assert is_text_mime_type(yaml_mime) is True

        # PDF
        pdf_mime = detect_mime_type(b"%PDF-1.4", "doc.pdf")
        assert is_pdf_mime_type(pdf_mime) is True
        assert is_text_mime_type(pdf_mime) is False

        # Image
        png_mime = detect_mime_type(b"\x89PNG\r\n\x1a\n", "img.png")
        assert is_image_mime_type(png_mime) is True
        assert is_text_mime_type(png_mime) is False

    def test_heic_heif_extension_detection(self):
        """Test HEIC/HEIF extension-based MIME type detection."""
        assert detect_mime_type(b"\x00\x00\x00", "photo.heic") == "image/heic"
        assert detect_mime_type(b"\x00\x00\x00", "photo.heif") == "image/heif"
