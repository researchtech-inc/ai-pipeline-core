"""Prove that ai-replay run discards document content.

_serialize_result extracts only name/sha256 (not content), _write_output creates
no files/ directory, and _run_span never passes sink_db to execute_span.
"""

# pyright: reportPrivateUsage=false

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import yaml

from ai_pipeline_core.documents.document import Document
from ai_pipeline_core.replay.cli import _serialize_result, _write_output


class ContentDoc(Document):
    """Document subclass for replay content loss tests."""


# ── _serialize_result strips content ─────────────────────────────────


class TestSerializeResultDiscardsContent:
    """Prove that _serialize_result strips document content."""

    def test_single_document_has_no_content_field(self) -> None:
        """Passes on current code — demonstrates content is lost."""
        doc = ContentDoc.create_root(
            name="report.md",
            content="# Important Report\n\nCritical findings here.",
            reason="test",
        )
        serialized = _serialize_result(doc)

        assert serialized["type"] == "document"
        assert serialized["name"] == "report.md"
        assert "sha256" in serialized
        assert "content" not in serialized

    def test_document_tuple_has_no_content_fields(self) -> None:
        """Passes on current code — demonstrates content is lost for tuples."""
        docs = (
            ContentDoc.create_root(name="part1.md", content="Part 1 body", reason="test"),
            ContentDoc.create_root(name="part2.json", content='{"key": "value"}', reason="test"),
        )
        serialized = _serialize_result(docs)

        assert serialized["type"] == "document_list"
        assert len(serialized["documents"]) == 2
        for doc_entry in serialized["documents"]:
            assert "name" in doc_entry
            assert "sha256" in doc_entry
            assert "content" not in doc_entry

    def test_content_string_absent_from_serialized_output(self) -> None:
        """The actual content does not appear anywhere in serialized output."""
        unique_content = "UNIQUE_MARKER_7f3a2b_content_that_must_survive"
        doc = ContentDoc.create_root(name="data.txt", content=unique_content, reason="test")
        serialized = _serialize_result(doc)

        serialized_text = str(serialized)
        assert unique_content not in serialized_text


# ── _write_output creates no files/ subdirectory ─────────────────────


class TestWriteOutputNoFilesDir:
    """Prove _write_output does not create a files/ subdirectory."""

    def test_no_files_directory_created(self, tmp_path: Path) -> None:
        """Passes on current code — document files are not written."""
        doc = ContentDoc.create_root(name="report.md", content="# Report content", reason="test")
        _write_output(tmp_path, (doc,))

        assert (tmp_path / "output.yaml").is_file()
        assert not (tmp_path / "files").exists()

    def test_yaml_contains_no_document_content(self, tmp_path: Path) -> None:
        """output.yaml has SHA256 references but not actual content."""
        content_text = "This important content should survive replay."
        doc = ContentDoc.create_root(name="important.md", content=content_text, reason="test")
        _write_output(tmp_path, (doc,))

        full_yaml = (tmp_path / "output.yaml").read_text(encoding="utf-8")
        assert content_text not in full_yaml

    def test_yaml_roundtrips_with_sha_only(self, tmp_path: Path) -> None:
        """Parsed YAML has sha256 but no content — dead reference."""
        doc = ContentDoc.create_root(name="data.json", content='{"key": 1}', reason="test")
        _write_output(tmp_path, doc)

        parsed = yaml.safe_load((tmp_path / "output.yaml").read_text(encoding="utf-8"))
        assert parsed["type"] == "document"
        assert "sha256" in parsed
        assert parsed.get("content") is None


# ── _run_span omits sink_db ──────────────────────────────────────────


class TestRunSpanNoSinkDb:
    """Prove _run_span does not forward sink_db to execute_span."""

    @pytest.mark.asyncio
    async def test_run_span_calls_execute_span_without_sink_db(self) -> None:
        """Passes on current code — sink_db is never set."""
        from ai_pipeline_core.replay.cli import _run_span

        captured_kwargs: dict[str, Any] = {}

        async def mock_execute_span(span_id: Any, *, source_db: Any, sink_db: Any = None) -> ContentDoc:
            captured_kwargs["source_db"] = source_db
            captured_kwargs["sink_db"] = sink_db
            return ContentDoc.create_root(name="out.md", content="result", reason="test")

        mock_db = AsyncMock()

        with patch("ai_pipeline_core.replay.cli.execute_span", side_effect=mock_execute_span):
            await _run_span(span_id=uuid4(), database=mock_db)

        assert captured_kwargs["sink_db"] is None
        assert captured_kwargs["source_db"] is mock_db
