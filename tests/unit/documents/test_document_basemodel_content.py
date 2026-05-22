"""Prove Document factory methods already accept BaseModel content.

derive(), create(), create_root(), and create_external() handle BaseModel
content via _convert_content(). No separate from_model() is needed.
"""

import json

from pydantic import BaseModel

from ai_pipeline_core.documents.document import Document


class ContentModel(BaseModel, frozen=True):
    """Content model for factory method tests."""

    summary: str
    score: float
    tags: list[str]


class SourceDoc(Document):
    """Source for provenance."""


class AnalysisDoc(Document):
    """Document containing analysis results."""


class TestBaseModelContentSupported:
    """Prove: existing factory methods accept BaseModel content."""

    def test_derive_accepts_basemodel_content(self) -> None:
        source = SourceDoc.create_root(name="source.md", content="data", reason="test")
        model = ContentModel(summary="analysis", score=0.95, tags=["ai", "ml"])
        doc = AnalysisDoc.derive(name="analysis.json", content=model, derived_from=(source,))

        parsed = json.loads(doc.text)
        assert parsed["summary"] == "analysis"
        assert parsed["score"] == 0.95

    def test_derive_accepts_list_of_basemodels(self) -> None:
        source = SourceDoc.create_root(name="source.md", content="data", reason="test")
        models = [
            ContentModel(summary="first", score=0.8, tags=["a"]),
            ContentModel(summary="second", score=0.6, tags=["b"]),
        ]
        doc = AnalysisDoc.derive(name="results.json", content=models, derived_from=(source,))

        parsed = json.loads(doc.text)
        assert isinstance(parsed, list)
        assert len(parsed) == 2

    def test_create_root_accepts_basemodel(self) -> None:
        model = ContentModel(summary="root", score=1.0, tags=["root"])
        doc = AnalysisDoc.create_root(name="config.json", content=model, reason="test")
        parsed = json.loads(doc.text)
        assert parsed["summary"] == "root"

    def test_create_accepts_basemodel(self) -> None:
        trigger = SourceDoc.create_root(name="trigger.md", content="x", reason="test")
        model = ContentModel(summary="created", score=0.5, tags=["new"])
        doc = AnalysisDoc.create(name="output.json", content=model, triggered_by=(trigger,))
        parsed = json.loads(doc.text)
        assert parsed["summary"] == "created"

    def test_create_external_accepts_basemodel(self) -> None:
        model = ContentModel(summary="external", score=0.7, tags=["web"])
        doc = AnalysisDoc.create_external(
            name="fetched.json",
            content=model,
            from_sources=["https://example.com/api"],
        )
        parsed = json.loads(doc.text)
        assert parsed["summary"] == "external"

    def test_basemodel_roundtrips_via_as_pydantic_model(self) -> None:
        source = SourceDoc.create_root(name="s.md", content="x", reason="test")
        original = ContentModel(summary="roundtrip", score=0.99, tags=["a", "b"])
        doc = AnalysisDoc.derive(name="roundtrip.json", content=original, derived_from=(source,))
        recovered = doc.as_pydantic_model(ContentModel)
        assert recovered == original
