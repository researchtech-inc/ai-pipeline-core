"""Tests for Document.summary field."""

from ai_pipeline_core.documents import Document


class _SummaryDoc(Document):
    """Test document for inline summary tests."""


def test_create_root_with_summary() -> None:
    doc = _SummaryDoc.create_root(name="test.txt", content=b"hello", reason="test", summary="A summary")
    assert doc.summary == "A summary"


def test_create_root_without_summary_defaults_empty() -> None:
    doc = _SummaryDoc.create_root(name="test.txt", content=b"hello", reason="test")
    assert doc.summary == ""


def test_create_with_summary() -> None:
    source = _SummaryDoc.create_root(name="source.txt", content=b"source", reason="test input")
    doc = _SummaryDoc.derive(
        name="test.txt",
        content="hello",
        derived_from=(source,),
        summary="Auto-generated summary",
    )
    assert doc.summary == "Auto-generated summary"


def test_create_without_summary_defaults_empty() -> None:
    source = _SummaryDoc.create_root(name="source.txt", content=b"source", reason="test input")
    doc = _SummaryDoc.derive(
        name="test.txt",
        content="hello",
        derived_from=(source,),
    )
    assert doc.summary == ""


def test_summary_survives_serialization_roundtrip() -> None:
    doc = _SummaryDoc.create_root(name="test.txt", content=b"hello", reason="test", summary="roundtrip")
    serialized = doc.serialize_model()
    restored = _SummaryDoc.model_validate(serialized)
    assert restored.summary == "roundtrip"
