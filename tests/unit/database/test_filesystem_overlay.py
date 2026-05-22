"""Tests for create_debug_sink factory."""

import json

import pytest

from ai_pipeline_core.database.filesystem._backend import FilesystemDatabase
from ai_pipeline_core.database.filesystem.overlay import create_debug_sink


class TestCreateDebugSink:
    def test_creates_database(self, tmp_path) -> None:
        db = create_debug_sink(tmp_path / "out")
        assert isinstance(db, FilesystemDatabase)

    def test_writes_overlay_meta_with_parent(self, tmp_path) -> None:
        parent_path = tmp_path / "parent"
        parent = FilesystemDatabase(parent_path)
        sink = create_debug_sink(tmp_path / "overlay", parent=parent)
        assert isinstance(sink, FilesystemDatabase)

        meta_path = tmp_path / "overlay" / "overlay_meta.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text())
        assert meta["parent_path"] == str(parent_path.resolve())
        assert meta["type"] == "overlay"
        assert "created_at" in meta

    def test_no_parent_no_meta(self, tmp_path) -> None:
        create_debug_sink(tmp_path / "out")
        assert not (tmp_path / "out" / "overlay_meta.json").exists()

    def test_rejects_existing_spans_dir(self, tmp_path) -> None:
        output = tmp_path / "exists"
        (output / "spans").mkdir(parents=True)
        with pytest.raises(FileExistsError, match="already contains database artifacts"):
            create_debug_sink(output)

    def test_rejects_existing_documents_dir(self, tmp_path) -> None:
        output = tmp_path / "exists"
        (output / "documents").mkdir(parents=True)
        with pytest.raises(FileExistsError, match="already contains database artifacts"):
            create_debug_sink(output)

    def test_allows_empty_existing_dir(self, tmp_path) -> None:
        output = tmp_path / "exists"
        output.mkdir()
        (output / "notes.txt").write_text("hello")
        db = create_debug_sink(output)
        assert isinstance(db, FilesystemDatabase)

    def test_allows_nonexistent_dir(self, tmp_path) -> None:
        db = create_debug_sink(tmp_path / "new" / "deep" / "out")
        assert isinstance(db, FilesystemDatabase)
