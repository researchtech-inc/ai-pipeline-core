"""Prove the provenance API allows silent misuse.

create_external() does not require triggered_by. create_root() inside
a task context produces no warning about severed provenance.
"""

import warnings

from ai_pipeline_core.documents.document import Document
from ai_pipeline_core.pipeline._execution_context import pipeline_test_context

import pytest


class EvidenceDoc(Document):
    """Source evidence document."""


class FetchedDoc(Document):
    """Document fetched from external source."""


class ReportDoc(Document):
    """Final report document."""


class TestCreateExternalMissingTriggeredBy:
    """Prove: create_external() silently accepts missing triggered_by."""

    def test_create_external_without_triggered_by_succeeds(self) -> None:
        """Passes on current code — triggered_by defaults to None."""
        doc = FetchedDoc.create_external(
            name="page.html",
            content=b"<html><body>content</body></html>",
            from_sources=["https://example.com/page"],
        )
        assert doc.name == "page.html"
        assert doc.triggered_by == ()

    def test_create_external_with_triggered_by_works(self) -> None:
        """Positive case — triggered_by is provided."""
        trigger = EvidenceDoc.create_root(name="plan.md", content="plan", reason="test")
        doc = FetchedDoc.create_external(
            name="page.html",
            content=b"<html>body</html>",
            from_sources=["https://example.com"],
            triggered_by=(trigger,),
        )
        assert len(doc.triggered_by) == 1


class TestCreateRootInsideTaskContext:
    """Prove: create_root() inside task context produces no warning."""

    def test_no_provenance_warning_inside_context(self) -> None:
        """Passes on current code — no ProvenanceWarning class exists."""
        with pipeline_test_context():
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                ReportDoc.create_root(
                    name="report.md",
                    content="# Report with severed provenance",
                    reason="synthesis output",
                )
            provenance_warnings = [w for w in caught if "Provenance" in str(w.category.__name__)]
            assert len(provenance_warnings) == 0


class TestProvenanceNotImplemented:
    """Verify provenance tightening after implementation."""

    @pytest.mark.xfail(reason="triggered_by not yet required on create_external", strict=True)
    def test_create_external_without_triggered_by_raises(self) -> None:
        """After fix: omitting triggered_by should raise."""
        with pytest.raises((TypeError, ValueError)):
            FetchedDoc.create_external(
                name="page.html",
                content=b"<html>content</html>",
                from_sources=["https://example.com/page"],
            )

    @pytest.mark.xfail(reason="ProvenanceWarning not yet implemented", strict=True)
    def test_provenance_warning_class_exists(self) -> None:
        """After fix: ProvenanceWarning should be importable."""
        from ai_pipeline_core.documents.document import ProvenanceWarning  # type: ignore[attr-defined]

        assert issubclass(ProvenanceWarning, UserWarning)
