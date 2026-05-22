"""Tests for ``PromptResult.citations`` merging engine + parsed-response citations.

Pins three reviewer-flagged gaps:
- engine URL citations land with ``field=None``
- parsed ``CitedText`` citations land with ``field`` set to the dotted path
- engine ``start_index=0`` / ``end_index=0`` survive the mapping (H2 regression)
"""

from typing import Any, ClassVar
from unittest.mock import patch as _patch

import pytest
from pydantic import BaseModel

from ai_pipeline_core import FrozenBaseModel
from ai_pipeline_core._llm_core.model_response import Citation, ModelResponse
from ai_pipeline_core._llm_core.types import TokenUsage
from ai_pipeline_core.prompt_contract import CitedText, DocumentCitation, PromptContract


class OutputWithBody(FrozenBaseModel):
    """Output with one CitedText field."""

    body: CitedText
    label: str


class CitationsContract(PromptContract[OutputWithBody]):
    """Contract whose output carries CitedText for citation-collection tests."""

    purpose: ClassVar[str] = "test citation collection"
    returns: ClassVar[str] = "OutputWithBody"
    success_criteria: ClassVar[str] = "anything"


class Item(FrozenBaseModel):
    """One finding item."""

    summary: CitedText


class NestedOutput(FrozenBaseModel):
    """Nested output with a tuple of CitedText-bearing models."""

    findings: tuple[Item, ...]


class NestedCitationsContract(PromptContract[NestedOutput]):
    """Contract for nested-path tests."""

    purpose: ClassVar[str] = "test nested citation collection"
    returns: ClassVar[str] = "NestedOutput"
    success_criteria: ClassVar[str] = "anything"


def _make_response(parsed: BaseModel, *, citations: tuple[Citation, ...] = ()) -> ModelResponse[Any]:
    return ModelResponse[Any](
        content=parsed.model_dump_json(),
        parsed=parsed,
        usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        cost=0.0,
        model="test-model",
        response_id="r",
        citations=citations,
    )


def _patch_generate(monkeypatch: pytest.MonkeyPatch, response: ModelResponse[Any]) -> None:
    async def fake_generate(request: Any, *, response_type: Any = None) -> ModelResponse[Any]:
        _ = (request, response_type)
        return response

    from ai_pipeline_core.llm import _engine

    monkeypatch.setattr(_engine, "generate", fake_generate)


@pytest.mark.asyncio
async def test_engine_citations_only_carry_field_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """ModelResponse.citations map into PromptResult.citations with field=None."""
    from tests.support.model_catalog import DEFAULT_TEST_MODEL

    parsed = OutputWithBody(body=CitedText(text="No model-side citations."), label="x")
    engine_citation = Citation(title="Source A", url="https://a.example", start_index=10, end_index=20)
    _patch_generate(monkeypatch, _make_response(parsed, citations=(engine_citation,)))

    result = await CitationsContract().execute(DEFAULT_TEST_MODEL)

    assert len(result.citations) == 1
    citation = result.citations[0]
    assert isinstance(citation, DocumentCitation)
    assert citation.url == "https://a.example"
    assert citation.title == "Source A"
    assert citation.start_index == 10
    assert citation.end_index == 20
    assert citation.field is None


@pytest.mark.asyncio
async def test_cited_text_citations_collected_with_field_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Parsed CitedText.citations land on PromptResult with field=<field-name>."""
    from tests.support.model_catalog import DEFAULT_TEST_MODEL

    parsed = OutputWithBody(
        body=CitedText(
            text="The capital is Paris.",
            citations=(DocumentCitation(document_id="ev-001"), DocumentCitation(document_id="ev-002")),
        ),
        label="x",
    )
    _patch_generate(monkeypatch, _make_response(parsed))

    result = await CitationsContract().execute(DEFAULT_TEST_MODEL)

    fields = {(c.field, c.document_id) for c in result.citations}
    assert fields == {("body", "ev-001"), ("body", "ev-002")}


@pytest.mark.asyncio
async def test_nested_field_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tuple/nested CitedText fields produce dotted+indexed paths."""
    from tests.support.model_catalog import DEFAULT_TEST_MODEL

    parsed = NestedOutput(
        findings=(
            Item(summary=CitedText(text="a", citations=(DocumentCitation(document_id="ev-A"),))),
            Item(summary=CitedText(text="b", citations=(DocumentCitation(document_id="ev-B"),))),
        ),
    )
    _patch_generate(monkeypatch, _make_response(parsed))

    result = await NestedCitationsContract().execute(DEFAULT_TEST_MODEL)

    fields = {(c.field, c.document_id) for c in result.citations}
    assert fields == {("findings[0].summary", "ev-A"), ("findings[1].summary", "ev-B")}


@pytest.mark.asyncio
async def test_engine_plus_parsed_citations_combined_and_deduped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Engine and parsed citations co-exist; identical (field, ids) collapse via dedupe."""
    from tests.support.model_catalog import DEFAULT_TEST_MODEL

    parsed = OutputWithBody(
        body=CitedText(
            text="x", citations=(DocumentCitation(document_id="ev-001"), DocumentCitation(document_id="ev-001"))
        ),
        label="x",
    )
    engine_citation = Citation(title="t", url="https://x", start_index=0, end_index=0)
    _patch_generate(monkeypatch, _make_response(parsed, citations=(engine_citation, engine_citation)))

    result = await CitationsContract().execute(DEFAULT_TEST_MODEL)

    # 1 engine citation (deduped) + 1 parsed citation (deduped) = 2 total.
    assert len(result.citations) == 2
    engine_only = [c for c in result.citations if c.field is None]
    parsed_only = [c for c in result.citations if c.field == "body"]
    assert len(engine_only) == 1
    assert len(parsed_only) == 1


@pytest.mark.asyncio
async def test_engine_zero_offsets_preserved_in_prompt_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """H2 regression pin: engine citation with start_index=0/end_index=0 survives mapping."""
    from tests.support.model_catalog import DEFAULT_TEST_MODEL

    parsed = OutputWithBody(body=CitedText(text="x"), label="y")
    engine_citation = Citation(title="grounding", url="https://g.example", start_index=0, end_index=0)
    _patch_generate(monkeypatch, _make_response(parsed, citations=(engine_citation,)))

    result = await CitationsContract().execute(DEFAULT_TEST_MODEL)

    assert len(result.citations) == 1
    assert result.citations[0].start_index == 0
    assert result.citations[0].end_index == 0
    assert result.citations[0].url == "https://g.example"


# Avoid unused-import warnings if pyright sweeps.
_ = _patch
