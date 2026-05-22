"""Provenance patterns: correct use of derived_from vs triggered_by, derive() vs create()."""

import pytest
from pydantic import BaseModel

from ai_pipeline_core.documents import Document


# -- Domain models simulating a research pipeline --


class ResearchSpec(BaseModel):
    categories: dict[str, list[str]]  # category_name -> fields to research


class TrackerItem(BaseModel):
    category: str
    status: str
    findings: str


class GapTracker(BaseModel):
    iteration: int
    items: list[TrackerItem]


class OutputReport(BaseModel):
    sections: dict[str, list[dict[str, str]]]


class UploadManifest(BaseModel):
    uploads: list[dict[str, str]]  # [{filename, drive_url, drive_id}]


class FilterResult(BaseModel):
    excluded: list[str]
    reason: str


# -- Document subclasses --


class SpecDocument(Document[ResearchSpec]):
    pass


class ResearchDocument(Document):
    pass


class GapTrackerDocument(Document[GapTracker]):
    pass


class OutputDocument(Document[OutputReport]):
    pass


class ManifestDocument(Document[UploadManifest]):
    pass


class FilterResultDocument(Document[FilterResult]):
    pass


class AnalyzedDocument(Document):
    pass


@pytest.mark.ai_docs
def test_provenance_patterns():
    """Pipeline provenance: when to use derived_from vs triggered_by, derive() vs create().

    Models a research pipeline to demonstrate every provenance pattern.
    The field-tracing test: does the input document's content appear in the output?
    If yes -> derived_from. If no -> triggered_by.
    """

    # ── 1. Root inputs: pipeline entry points with no provenance ──

    spec_doc = SpecDocument.create_root(
        name="spec.json",
        content=ResearchSpec(categories={"financials": ["revenue", "profit"], "team": ["headcount"]}),
        reason="user-provided research specification",
    )
    raw_data_doc = ResearchDocument.create_root(name="raw_data.md", content="Company X had $10M revenue...", reason="uploaded by user")

    assert spec_doc.derived_from == ()
    assert spec_doc.triggered_by == ()

    # ── 2. Content transformation: input content is reused in output -> derived_from ──
    # The research doc's text is parsed and restructured into tracker items.
    # Content flows from input to output — this is derived_from.

    tracker_v1 = GapTrackerDocument.derive(
        name="tracker.json",
        content=GapTracker(
            iteration=1,
            items=[
                TrackerItem(category="financials", status="found", findings="$10M revenue from raw data"),
                TrackerItem(category="team", status="gap", findings=""),
            ],
        ),
        derived_from=(raw_data_doc,),
        triggered_by=(spec_doc,),  # spec says what to look for, but its content isn't in the tracker items
    )

    assert raw_data_doc.sha256 in tracker_v1.derived_from
    assert spec_doc.sha256 in tracker_v1.triggered_by

    # ── 3. State evolution: parse -> mutate -> serialize back -> derived_from ──
    # The new tracker IS the old tracker with modifications.
    # Old tracker content is the structural foundation of the output.

    old_tracker = tracker_v1.parsed
    new_items = []
    for item in old_tracker.items:
        if item.category == "team":
            new_items.append(TrackerItem(category="team", status="found", findings="150 employees from new research"))
        else:
            new_items.append(item)
    updated_tracker = GapTracker(iteration=old_tracker.iteration + 1, items=new_items)

    new_research_doc = ResearchDocument.create_root(name="team_research.md", content="Company X has 150 employees...", reason="web capture")

    tracker_v2 = GapTrackerDocument.derive(
        name="tracker.json",
        content=updated_tracker,
        # tracker_v1 content IS the output's foundation — derived_from, not triggered_by
        # new_research_doc findings flow into item.findings — also derived_from
        derived_from=(tracker_v1, new_research_doc),
    )

    assert tracker_v1.sha256 in tracker_v2.derived_from
    assert new_research_doc.sha256 in tracker_v2.derived_from

    # ── 4. Spec as structure: spec categories become output JSON keys -> derived_from ──
    # When spec.categories are iterated to build the output skeleton,
    # the spec's content (category names, field names) IS the output structure.

    tracker = tracker_v2.parsed
    sections: dict[str, list[dict[str, str]]] = {}
    for cat_name, fields in spec_doc.parsed.categories.items():
        items = [i for i in tracker.items if i.category == cat_name]
        sections[cat_name] = [{f: item.findings for f in fields} for item in items]

    output_doc = OutputDocument.derive(
        name="report.json",
        content=OutputReport(sections=sections),
        # Both tracker and spec content appear in the output:
        # tracker provides item findings, spec provides category/field structure
        derived_from=(tracker_v2, spec_doc),
    )

    assert tracker_v2.sha256 in output_doc.derived_from
    assert spec_doc.sha256 in output_doc.derived_from

    # ── 5. Criteria-driven decision: report provides filter criteria -> triggered_by ──
    # The report tells us what to exclude, but excluded doc names come from analyzed_docs.
    # Report content does NOT appear in the output — only the decision does.

    analyzed_docs = (
        AnalyzedDocument.create_root(name="doc_a.md", content="Relevant company data", reason="analysis input"),
        AnalyzedDocument.create_root(name="doc_b.md", content="Unrelated press release", reason="analysis input"),
    )

    filter_doc = FilterResultDocument.derive(
        name="filter.json",
        content=FilterResult(excluded=["doc_b.md"], reason="not relevant to financials"),
        derived_from=analyzed_docs,  # excluded names come from these documents
        triggered_by=(output_doc,),  # report provides criteria but its content isn't in the filter output
    )

    assert all(d.sha256 in filter_doc.derived_from for d in analyzed_docs)
    assert output_doc.sha256 in filter_doc.triggered_by

    # ── 6. Execution result: new data from external API -> create(), not derive() ──
    # Upload manifest contains Drive URLs and file IDs — new operational data.
    # The report was uploaded but its content doesn't appear in the manifest.

    manifest_doc = ManifestDocument.create(
        name="manifest.json",
        content=UploadManifest(uploads=[{"filename": "report.json", "drive_url": "https://drive.google.com/file/d/abc123", "drive_id": "abc123"}]),
        triggered_by=(output_doc,),  # report caused the upload, but manifest content is from Drive API
    )

    assert output_doc.sha256 in manifest_doc.triggered_by
    assert manifest_doc.derived_from == ()  # no content was derived — all new API data

    # ── 7. Overlap rejection: same document cannot be both derived_from and triggered_by ──

    with pytest.raises(ValueError, match="both derived_from and triggered_by"):
        OutputDocument.derive(
            name="bad.json",
            content=OutputReport(sections={}),
            derived_from=(tracker_v2,),
            triggered_by=(tracker_v2,),  # same doc in both — rejected
        )
