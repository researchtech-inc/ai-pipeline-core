"""Tests for document utilities."""

import pytest

from ai_pipeline_core.documents import Document
from ai_pipeline_core.documents.utils import ensure_extension, find_document, replace_extension, sanitize_url
from ai_pipeline_core.exceptions import DocumentNameError, DocumentValidationError


class TestSanitizeUrl:
    """Test URL sanitization for filenames."""

    def test_url_with_protocol(self):
        """Test sanitization of URLs with protocol."""
        assert sanitize_url("https://example.com/path") == "example.com_path"
        assert sanitize_url("http://test.org/file.pdf") == "test.org_file.pdf"

    def test_invalid_filename_characters(self):
        """Test removal of invalid filename characters."""
        assert sanitize_url('file<>:"/\\|?*name') == "file_name"
        assert sanitize_url("path/to/file") == "path_to_file"
        assert sanitize_url("file:name") == "file_name"

    def test_multiple_underscores(self):
        """Test collapsing of multiple underscores."""
        assert sanitize_url("file___name") == "file_name"
        assert sanitize_url("path//to//file") == "path_to_file"

    def test_trim_edges(self):
        """Test trimming of leading/trailing underscores and dots."""
        assert sanitize_url("_file_") == "file"
        assert sanitize_url(".file.") == "file"
        assert sanitize_url("__file__") == "file"

    def test_length_limit(self):
        """Test that long names are truncated to 100 characters."""
        long_name = "a" * 150
        result = sanitize_url(long_name)
        assert len(result) == 100
        assert result == "a" * 100

    def test_empty_fallback(self):
        """Test fallback to 'unnamed' for empty results."""
        assert sanitize_url("") == "unnamed"
        assert sanitize_url("...") == "unnamed"
        assert sanitize_url("___") == "unnamed"
        assert sanitize_url("//:") == "unnamed"

    def test_complex_urls(self):
        """Test complex URL patterns."""
        # URLs with query strings - query part is ignored when parsing
        assert sanitize_url("https://api.example.com/v1/resource?id=123&type=pdf") == "api.example.com_v1_resource"
        # FTP URLs aren't handled by the http/https check, @ is preserved
        assert sanitize_url("ftp://user:pass@host.com/file.txt") == "ftp_user_pass@host.com_file.txt"

    def test_query_strings(self):
        """Test handling of query strings without protocol."""
        # Query strings without protocol preserve ? and &
        assert sanitize_url("search?q=test&page=1") == "search_q=test&page=1"
        assert sanitize_url("file.php?download=true") == "file.php_download=true"


class TestEnsureExtension:
    def test_adds_missing_extension(self):
        assert ensure_extension("report", ".md") == "report.md"

    def test_no_double_extension(self):
        assert ensure_extension("report.md", ".md") == "report.md"

    def test_different_extension_appended(self):
        assert ensure_extension("report.txt", ".md") == "report.txt.md"

    def test_without_dot_prefix(self):
        assert ensure_extension("report", "md") == "report.md"

    def test_empty_name(self):
        assert ensure_extension("", ".md") == ".md"


class TestReplaceExtension:
    def test_replaces_extension(self):
        assert replace_extension("report.txt", ".md") == "report.md"

    def test_adds_when_no_extension(self):
        assert replace_extension("report", ".md") == "report.md"

    def test_without_dot_prefix(self):
        assert replace_extension("report.txt", "md") == "report.md"

    def test_compound_extension(self):
        assert replace_extension("archive.tar.gz", ".zip") == "archive.tar.zip"


class _DoubleExtDoc(Document):
    pass


class _TypeA(Document):
    pass


class _TypeB(Document):
    pass


class TestDoubleExtensionDetection:
    def test_rejects_double_md(self):
        with pytest.raises(DocumentNameError, match="Double extension"):
            _DoubleExtDoc.create_root(name="report.md.md", content="test", reason="test input")

    def test_rejects_double_json(self):
        with pytest.raises(DocumentNameError, match="Double extension"):
            _DoubleExtDoc.create_root(name="data.json.json", content="{}", reason="test input")

    def test_allows_different_extensions(self):
        doc = _DoubleExtDoc.create_root(name="report.txt.md", content="test", reason="test input")
        assert doc.name == "report.txt.md"

    def test_allows_single_extension(self):
        doc = _DoubleExtDoc.create_root(name="report.md", content="test", reason="test input")
        assert doc.name == "report.md"


class TestFindDocument:
    def test_finds_by_type(self):
        a = _TypeA.create_root(name="a.txt", content="aaa", reason="test input")
        b = _TypeB.create_root(name="b.txt", content="bbb", reason="test input")
        result = find_document([a, b], _TypeA)
        assert result is a

    def test_raises_on_missing_type(self):
        a = _TypeA.create_root(name="a.txt", content="aaa", reason="test input")
        with pytest.raises(DocumentValidationError, match="_TypeB"):
            find_document([a], _TypeB)

    def test_error_lists_available_types(self):
        a = _TypeA.create_root(name="a.txt", content="aaa", reason="test input")
        with pytest.raises(DocumentValidationError, match="_TypeA"):
            find_document([a], _TypeB)

    def test_empty_list(self):
        with pytest.raises(DocumentValidationError, match="none"):
            find_document([], _TypeA)

    def test_returns_first_match(self):
        a1 = _TypeA.create_root(name="a1.txt", content="first", reason="test input")
        a2 = _TypeA.create_root(name="a2.txt", content="second", reason="test input")
        result = find_document([a1, a2], _TypeA)
        assert result is a1
