"""Tests for ``CitedText`` and ``DocumentCitation`` (Stage A surface additions)."""

import pytest
from pydantic import ValidationError

from ai_pipeline_core import CitedText, DocumentCitation, FrozenBaseModel


class TestDocumentCitation:
    def test_defaults_all_none(self) -> None:
        citation = DocumentCitation()
        assert citation.document_id is None
        assert citation.url is None
        assert citation.title is None
        assert citation.start_index is None
        assert citation.end_index is None

    def test_constructed_with_url(self) -> None:
        citation = DocumentCitation(url="https://example.com", title="Example")
        assert citation.url == "https://example.com"
        assert citation.title == "Example"

    def test_constructed_with_document_id(self) -> None:
        citation = DocumentCitation(document_id="ev-001", start_index=5, end_index=10)
        assert citation.document_id == "ev-001"
        assert citation.start_index == 5
        assert citation.end_index == 10

    def test_is_frozen(self) -> None:
        citation = DocumentCitation(url="https://x.com")
        with pytest.raises(ValidationError):
            citation.url = "https://y.com"  # type: ignore[misc]

    def test_extra_keys_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            DocumentCitation(unknown_field="x")  # type: ignore[call-arg]

    def test_subclass_of_frozen_base_model(self) -> None:
        assert issubclass(DocumentCitation, FrozenBaseModel)

    def test_json_round_trip(self) -> None:
        citation = DocumentCitation(document_id="ev-001", url="https://x.com", title="t", start_index=1, end_index=5, field="body")
        restored = DocumentCitation.model_validate_json(citation.model_dump_json())
        assert restored == citation

    def test_field_defaults_none(self) -> None:
        assert DocumentCitation().field is None

    def test_field_explicit(self) -> None:
        citation = DocumentCitation(document_id="ev-001", field="findings[0].summary")
        assert citation.field == "findings[0].summary"

    def test_zero_offsets_preserved(self) -> None:
        # Engine grounding/flat-URL citations emit start_index=0/end_index=0; those
        # must survive as integer zeros, not collapse to None.
        citation = DocumentCitation(url="https://x.com", start_index=0, end_index=0)
        restored = DocumentCitation.model_validate_json(citation.model_dump_json())
        assert restored.start_index == 0
        assert restored.end_index == 0


class TestCitedText:
    def test_defaults_empty_citations(self) -> None:
        cited = CitedText(text="Hello")
        assert cited.text == "Hello"
        assert cited.citations == ()

    def test_with_citations(self) -> None:
        cited = CitedText(
            text="The capital is Paris.",
            citations=(DocumentCitation(document_id="ev-001"),),
        )
        assert len(cited.citations) == 1
        assert cited.citations[0].document_id == "ev-001"

    def test_is_frozen(self) -> None:
        cited = CitedText(text="x")
        with pytest.raises(ValidationError):
            cited.text = "y"  # type: ignore[misc]

    def test_subclass_of_frozen_base_model(self) -> None:
        assert issubclass(CitedText, FrozenBaseModel)

    def test_json_round_trip(self) -> None:
        cited = CitedText(
            text="The answer is X.",
            citations=(
                DocumentCitation(document_id="ev-001"),
                DocumentCitation(url="https://x.com", title="T"),
            ),
        )
        restored = CitedText.model_validate_json(cited.model_dump_json())
        assert restored == cited
