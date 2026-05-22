"""Tests for ClickHouse DDL correctness."""

from ai_pipeline_core.database.clickhouse._ddl import BLOBS_DDL, DOCUMENTS_DDL, SCHEMA_VERSION


def test_blobs_ddl_uses_replacing_merge_tree() -> None:
    """Blobs table uses ReplacingMergeTree for content-addressed deduplication."""
    assert "ReplacingMergeTree()" in BLOBS_DDL


def test_documents_ddl_uses_replacing_merge_tree_without_version() -> None:
    """Documents table uses ReplacingMergeTree() without a version column."""
    assert "ReplacingMergeTree()" in DOCUMENTS_DDL


def test_schema_version_is_3() -> None:
    """Schema version bumped to 3 for created_at removal and engine changes."""
    assert SCHEMA_VERSION == 3


def test_blobs_ddl_has_no_created_at() -> None:
    """Blobs are content-addressed — no timestamp needed."""
    assert "created_at" not in BLOBS_DDL


def test_documents_ddl_has_no_created_at() -> None:
    """Documents are content-addressed — no timestamp needed."""
    assert "created_at" not in DOCUMENTS_DDL
