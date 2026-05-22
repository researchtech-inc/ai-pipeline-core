"""Citation extraction across provider-specific response shapes."""

from types import SimpleNamespace

from pydantic import BaseModel, ConfigDict

from ai_pipeline_core._llm_core._response_builder import _extract_citations
from ai_pipeline_core._llm_core.model_response import Citation


def _message(annotations: list[object] | None = None) -> SimpleNamespace:
    return SimpleNamespace(annotations=annotations or [])


def test_nested_url_citation_annotation_extracts() -> None:
    annotation = {
        "type": "url_citation",
        "url_citation": {
            "title": "Nested",
            "url": "https://example.com/nested",
            "start_index": 2,
            "end_index": 8,
        },
    }

    assert _extract_citations(_message([annotation])) == (Citation(title="Nested", url="https://example.com/nested", start_index=2, end_index=8),)


def test_top_level_responses_annotation_extracts() -> None:
    annotation = {
        "type": "url_citation",
        "url_citation": None,
        "title": "Top Level",
        "url": "https://example.com/top",
        "start_index": 4,
        "end_index": 12,
    }

    assert _extract_citations(_message([annotation])) == (Citation(title="Top Level", url="https://example.com/top", start_index=4, end_index=12),)


def test_pydantic_annotation_object_extracts() -> None:
    class UrlCitationPayload(BaseModel):
        title: str
        url: str
        start_index: int
        end_index: int

    class AnnotationPayload(BaseModel):
        type: str
        url_citation: UrlCitationPayload

    annotation = AnnotationPayload(
        type="url_citation",
        url_citation=UrlCitationPayload(title="Object", url="https://example.com/object", start_index=1, end_index=6),
    )

    assert _extract_citations(_message([annotation])) == (Citation(title="Object", url="https://example.com/object", start_index=1, end_index=6),)


def test_pydantic_extra_annotation_fields_extract() -> None:
    class AnnotationPayload(BaseModel):
        model_config = ConfigDict(extra="allow")

        type: str
        url_citation: None = None

    annotation = AnnotationPayload(
        type="url_citation",
        url="https://example.com/extra",
        title="Extra",
        start_index=3,
        end_index=9,
    )

    assert _extract_citations(_message([annotation])) == (Citation(title="Extra", url="https://example.com/extra", start_index=3, end_index=9),)


def test_camel_case_grounding_metadata_extracts() -> None:
    fields = {
        "groundingMetadata": {
            "groundingChunks": [
                {"web": {"uri": "https://example.com/grounding", "title": "Grounding"}},
            ]
        }
    }

    assert _extract_citations(_message(), fields) == (Citation(title="Grounding", url="https://example.com/grounding", start_index=0, end_index=0),)


def test_snake_case_grounding_metadata_extracts() -> None:
    fields = {
        "grounding_metadata": {
            "grounding_chunks": [
                {"web": {"url": "https://example.com/snake", "title": "Snake"}},
            ]
        }
    }

    assert _extract_citations(_message(), fields) == (Citation(title="Snake", url="https://example.com/snake", start_index=0, end_index=0),)


def test_flat_provider_citations_extract() -> None:
    fields = {"citations": ["https://example.com/one", "https://example.com/two"]}

    assert _extract_citations(_message(), fields) == (
        Citation(title="", url="https://example.com/one", start_index=0, end_index=0),
        Citation(title="", url="https://example.com/two", start_index=0, end_index=0),
    )


def test_annotations_take_precedence_over_provider_fields() -> None:
    annotation = {"type": "url_citation", "url": "https://example.com/annotation", "title": "Annotation"}
    fields = {
        "groundingMetadata": {
            "groundingChunks": [
                {"web": {"uri": "https://example.com/provider", "title": "Provider"}},
            ]
        }
    }

    assert _extract_citations(_message([annotation]), fields) == (
        Citation(title="Annotation", url="https://example.com/annotation", start_index=0, end_index=0),
    )


def test_annotation_without_grounding_chunks_preserves_indices() -> None:
    """The old zero-index fallback was removed; annotation indices are preserved as supplied."""
    annotation = {
        "type": "url_citation",
        "url": "https://example.com/provider-annotation",
        "title": "Provider Annotation",
        "start_index": 5,
        "end_index": 22,
    }
    fields = {"thought_signatures": ["signature"]}

    assert _extract_citations(_message([annotation]), fields) == (
        Citation(title="Provider Annotation", url="https://example.com/provider-annotation", start_index=5, end_index=22),
    )


def test_duplicate_annotations_are_deduplicated() -> None:
    annotation = {"type": "url_citation", "url": "https://example.com/dup", "title": "Dup"}

    assert _extract_citations(_message([annotation, annotation])) == (Citation(title="Dup", url="https://example.com/dup", start_index=0, end_index=0),)


def test_empty_when_no_citation_source() -> None:
    fields = {"groundingMetadata": {"groundingChunks": [{"web": {}}]}, "citations": [None, ""]}

    assert _extract_citations(_message([{"type": "other"}]), fields) == ()
