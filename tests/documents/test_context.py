"""Tests for RunContext and _TaskDocumentContext."""

import pytest

from ai_pipeline_core.documents import Document
from ai_pipeline_core.pipeline._execution_context import (
    _TaskDocumentContext,
    _run_context,
    set_run_context,
)
from ai_pipeline_core.pipeline._execution_context import _RunContext


# --- Concrete document subclass for testing ---


class SampleDoc(Document):
    pass


def _make_doc(
    name: str,
    content: str = "test",
    derived_from: tuple[str, ...] | None = None,
    triggered_by: tuple[str, ...] | None = None,
) -> SampleDoc:
    return SampleDoc(
        name=name,
        content=content.encode("utf-8"),
        derived_from=derived_from or (),
        triggered_by=triggered_by or (),
    )


# ===== RunContext tests =====


class TestRunContext:
    def test_creation(self):
        ctx = _RunContext(run_id="project/flow/run123")
        assert ctx.run_id == "project/flow/run123"

    def test_frozen(self):
        ctx = _RunContext(run_id="test")
        with pytest.raises(AttributeError):
            ctx.run_id = "changed"  # type: ignore[misc]

    def test_get_returns_none_by_default(self):
        assert _run_context.get() is None

    def test_set_and_get(self):
        ctx = _RunContext(run_id="my-run")
        with set_run_context(ctx):
            assert _run_context.get() is ctx

    def test_token_restores_previous(self):
        ctx1 = _RunContext(run_id="first")
        ctx2 = _RunContext(run_id="second")
        with set_run_context(ctx1):
            with set_run_context(ctx2):
                assert _run_context.get() is ctx2
            assert _run_context.get() is ctx1
        assert _run_context.get() is None

    def test_execution_id_defaults_to_none(self):
        ctx = _RunContext(run_id="test")
        assert ctx.execution_id is None

    def test_execution_id_stored(self):
        from uuid import uuid4

        uid = uuid4()
        ctx = _RunContext(run_id="test", execution_id=uid)
        assert ctx.execution_id == uid


# ===== validate_provenance =====


class TestValidateProvenance:
    def test_derived_from_in_existing_set(self):
        """derived_from reference exists in the store — no warning."""
        parent = _make_doc("parent.txt", "parent")
        doc = _make_doc("a.txt", "aaa", derived_from=(parent.sha256,))
        ctx = _TaskDocumentContext()
        warnings = ctx.validate_provenance([doc], existing_sha256s={parent.sha256})
        assert warnings == []

    def test_missing_derived_from(self):
        """Document references a SHA256 that doesn't exist in the store."""
        phantom = _make_doc("phantom.txt", "ghost")
        doc = _make_doc("a.txt", "aaa", derived_from=(phantom.sha256,))
        ctx = _TaskDocumentContext()
        warnings = ctx.validate_provenance([doc], existing_sha256s=set())
        assert len(warnings) == 1
        assert "does not exist" in warnings[0]

    def test_accepts_url_derived_from(self):
        """URLs in derived_from are accepted and not validated as SHA256."""
        doc = _make_doc("a.txt", "aaa", derived_from=("https://example.com",))
        ctx = _TaskDocumentContext()
        warnings = ctx.validate_provenance([doc], existing_sha256s=set())
        assert all("does not exist" not in w for w in warnings)

    def test_rejects_invalid_derived_from_strings(self):
        """Non-SHA256, non-URL strings are rejected at document creation."""
        with pytest.raises(Exception):
            _make_doc("a.txt", "aaa", derived_from=("not-a-hash",))
        with pytest.raises(Exception):
            _make_doc("a.txt", "aaa", derived_from=("short",))

    def test_missing_triggered_by(self):
        """Document references a triggered_by SHA256 that doesn't exist."""
        phantom = _make_doc("phantom.txt", "ghost")
        doc = _make_doc("a.txt", "aaa", triggered_by=(phantom.sha256,))
        ctx = _TaskDocumentContext()
        warnings = ctx.validate_provenance([doc], existing_sha256s=set())
        assert len(warnings) == 1
        assert "triggered_by" in warnings[0]
        assert "does not exist" in warnings[0]

    def test_triggered_by_in_existing_set(self):
        """triggered_by reference exists in the store — no warning."""
        parent = _make_doc("parent.txt", "parent")
        doc = _make_doc("a.txt", "aaa", triggered_by=(parent.sha256,))
        ctx = _TaskDocumentContext()
        warnings = ctx.validate_provenance([doc], existing_sha256s={parent.sha256})
        assert warnings == []

    def test_same_task_derived_from_interdep(self):
        """derived_from SHA256 created in the same task produces a warning."""
        doc_a = _make_doc("a.txt", "aaa")
        doc_b = _make_doc("b.txt", "bbb", derived_from=(doc_a.sha256,))
        ctx = _TaskDocumentContext()
        warnings = ctx.validate_provenance([doc_a, doc_b], existing_sha256s=set())
        same_task_warnings = [w for w in warnings if "same task" in w]
        assert len(same_task_warnings) == 1

    def test_same_task_triggered_by_interdep(self):
        """triggered_by SHA256 created in the same task produces a warning."""
        doc_a = _make_doc("a.txt", "aaa")
        doc_b = _make_doc("b.txt", "bbb", triggered_by=(doc_a.sha256,))
        ctx = _TaskDocumentContext()
        warnings = ctx.validate_provenance([doc_a, doc_b], existing_sha256s=set())
        same_task_warnings = [w for w in warnings if "same task" in w]
        assert len(same_task_warnings) == 1
        assert "triggered_by" in same_task_warnings[0]

    def test_no_provenance_warning(self):
        """Document with no derived_from and no triggered_by gets a warning."""
        doc = _make_doc("a.txt", "aaa")
        ctx = _TaskDocumentContext()
        warnings = ctx.validate_provenance([doc], existing_sha256s=set())
        assert len(warnings) == 1
        assert "no provenance" in warnings[0]

    def test_url_derived_from_no_provenance_warning(self):
        """Document with URL derived_from has provenance — no warning."""
        doc = _make_doc("a.txt", "aaa", derived_from=("https://example.com",))
        ctx = _TaskDocumentContext()
        warnings = ctx.validate_provenance([doc], existing_sha256s=set())
        assert warnings == []

    def test_mixed_valid_and_invalid(self):
        """Multiple documents with different provenance issues."""
        parent = _make_doc("parent.txt", "parent")
        valid = _make_doc("valid.txt", "valid", derived_from=(parent.sha256,))
        orphan = _make_doc("orphan.txt", "orphan")  # no provenance
        ctx = _TaskDocumentContext()
        warnings = ctx.validate_provenance([valid, orphan], existing_sha256s={parent.sha256})
        assert len(warnings) == 1
        assert "no provenance" in warnings[0]


# ===== deduplicate =====


class TestDeduplicate:
    def test_empty_list(self):
        assert _TaskDocumentContext.deduplicate([]) == []

    def test_no_duplicates(self):
        docs = [_make_doc("a.txt", "aaa"), _make_doc("b.txt", "bbb")]
        result = _TaskDocumentContext.deduplicate(docs)
        assert len(result) == 2

    def test_removes_duplicates(self):
        doc = _make_doc("a.txt", "aaa")
        result = _TaskDocumentContext.deduplicate([doc, doc])
        assert len(result) == 1
        assert result[0].name == "a.txt"

    def test_preserves_first_occurrence_order(self):
        doc_a = _make_doc("a.txt", "aaa")
        doc_b = _make_doc("b.txt", "bbb")
        doc_c = _make_doc("c.txt", "ccc")
        result = _TaskDocumentContext.deduplicate([doc_c, doc_a, doc_b, doc_a, doc_c])
        assert [d.name for d in result] == ["c.txt", "a.txt", "b.txt"]

    def test_same_content_different_names(self):
        """Documents with identical content but different names have the same SHA256."""
        doc1 = _make_doc("first.txt", "same content")
        doc2 = _make_doc("first.txt", "same content")
        result = _TaskDocumentContext.deduplicate([doc1, doc2])
        assert len(result) == 1
