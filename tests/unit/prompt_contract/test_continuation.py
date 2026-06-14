"""Offline tests for PromptContract continuation lineage and single-str prose mode.

These exercise the two behaviors that the live integration suite cannot pin
deterministically:

- The single-required-``str`` output model selects markdown (prose) transport:
  ``response_format`` is dropped and the model's plain text is wrapped back into
  the output model.
- A ``continues=`` follow-up reuses the opener's document prefix as the cacheable
  context (so the prompt-cache key is unchanged), threads the opener's prior
  turns, and appends any newly declared follow-up documents *after* those turns
  — never into the cache prefix.

``_engine.generate`` is patched so no network call is made.
"""

from typing import Any, ClassVar

import pytest
from pydantic import Field

from ai_pipeline_core import FrozenBaseModel
from ai_pipeline_core._llm_core.model_response import ModelResponse
from ai_pipeline_core._llm_core.request import LLMRequest
from ai_pipeline_core._llm_core.types import TokenUsage
from ai_pipeline_core.documents import Document
from ai_pipeline_core.llm import _engine
from ai_pipeline_core.prompt_contract import Continues, PromptContract, PromptResult
from tests.support.helpers import ConcreteDocument
from tests.support.model_catalog import DEFAULT_TEST_MODEL


def _patch_generate(monkeypatch: pytest.MonkeyPatch, scripted: list[ModelResponse[Any]]) -> list[LLMRequest]:
    captured: list[LLMRequest] = []
    iterator = iter(scripted)

    async def fake_generate(request: LLMRequest, *, response_type: Any = None) -> ModelResponse[Any]:
        _ = response_type
        captured.append(request)
        try:
            return next(iterator)
        except StopIteration as exc:  # pragma: no cover - defensive
            raise AssertionError("fake_generate called more times than scripted responses") from exc

    monkeypatch.setattr(_engine, "generate", fake_generate)
    return captured


def _prose_response(text: str) -> ModelResponse[Any]:
    """A response whose parsed payload is a plain string (markdown transport)."""
    return ModelResponse[Any](
        content=text,
        parsed=text,
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        cost=0.0,
        model="test-model",
        response_id="resp-prose",
    )


def _structured_response(parsed: FrozenBaseModel) -> ModelResponse[Any]:
    return ModelResponse[Any](
        content=parsed.model_dump_json(),
        parsed=parsed,
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        cost=0.0,
        model="test-model",
        response_id="resp-structured",
    )


# ---------------------------------------------------------------------------
# Single-required-str output => prose (markdown) transport.
# ---------------------------------------------------------------------------


class ProseOutput(FrozenBaseModel):
    """Output with a single required str field — selects prose transport."""

    body: str = Field(description="Free-form markdown answer.")


class ProseContract(PromptContract[ProseOutput]):
    """Contract whose output is a single markdown body."""

    purpose: ClassVar[str] = "Write a short paragraph."
    returns: ClassVar[str] = "A markdown paragraph."
    success_criteria: ClassVar[str] = "The paragraph is non-empty."

    topic: str = Field(description="Topic to write about.")


def test_prose_field_detected_at_class_definition() -> None:
    assert ProseContract._prose_field_name == "body"


@pytest.mark.asyncio
async def test_prose_mode_drops_response_format_and_wraps_text(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_generate(monkeypatch, [_prose_response("Rivers shape the land over time.")])

    result = await ProseContract(topic="rivers").execute(DEFAULT_TEST_MODEL)

    assert isinstance(result.response, ProseOutput)
    assert result.response.body == "Rivers shape the land over time."
    # Markdown transport: the engine is told NOT to request a structured format.
    assert captured[0].response.format is None
    # The rendered user prompt instructs the model to answer in markdown.
    payload = "\n".join(m.content if isinstance(m.content, str) else "" for m in captured[0].messages)
    assert "# Response Format" in payload
    assert "markdown" in payload.lower()


# ---------------------------------------------------------------------------
# Continuation: opener documents form the cache prefix; follow-up documents are
# appended after prior turns and do not change the cache key.
# ---------------------------------------------------------------------------


class DraftOutput(FrozenBaseModel):
    """Two-field output kept in structured mode for the continuation tests."""

    answer: str = Field(description="Draft answer.")
    score: int = Field(default=0, description="Confidence score.")


class DraftContract(PromptContract[DraftOutput]):
    """Opener contract that reads one source document."""

    purpose: ClassVar[str] = "Draft an answer from the source."
    returns: ClassVar[str] = "A DraftOutput."
    success_criteria: ClassVar[str] = "answer is non-empty."

    source: Document = Field(description="Source document to draft from.")


class ReviseContract(PromptContract[DraftOutput], continues=Continues.once(DraftContract)):
    """Continuation that revises the prior draft and reads an additional note document."""

    purpose: ClassVar[str] = "Revise the prior draft using the note."
    returns: ClassVar[str] = "A revised DraftOutput."
    success_criteria: ClassVar[str] = "answer reflects the note."

    prior: PromptResult[DraftOutput] = Field(description="The draft turn this revision continues.")
    note: Document = Field(description="Follow-up note appended after the prior turns.")


def test_continuation_lineage_recorded_at_class_definition() -> None:
    assert ReviseContract._continues is not None
    assert ReviseContract._continues.opener is DraftContract
    assert ReviseContract._predecessor_field_name == "prior"


@pytest.mark.asyncio
async def test_followup_documents_do_not_change_cache_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """The opener's document is the whole cache prefix for both turns; the follow-up
    document is appended after the threaded prior turns and never counted into the
    cacheable context."""
    from ai_pipeline_core._llm_core._transport import compute_cache_key, core_messages_to_api

    source = ConcreteDocument.create_root(name="source.txt", content="Source body text.", reason="cont-test")
    note = ConcreteDocument.create_root(name="note.txt", content="NOTE-SENTINEL-9 follow-up.", reason="cont-test")

    captured = _patch_generate(
        monkeypatch,
        [
            _structured_response(DraftOutput(answer="draft", score=1)),
            _structured_response(DraftOutput(answer="revised", score=2)),
        ],
    )

    opener_result = await DraftContract(source=source).execute(DEFAULT_TEST_MODEL)
    await ReviseContract(prior=opener_result, note=note).execute(DEFAULT_TEST_MODEL)

    assert len(captured) == 2
    opener_request, revise_request = captured

    # Both turns expose exactly one cacheable context message: the opener source doc.
    assert opener_request.cache.context_count == 1
    assert revise_request.cache.context_count == 1

    # The cache prefix is byte-identical across the two turns => same prompt-cache key.
    inline_cap = DEFAULT_TEST_MODEL.max_inline_file_total_bytes
    opener_api = core_messages_to_api(list(opener_request.messages), max_inline_file_total_bytes=inline_cap)
    revise_api = core_messages_to_api(list(revise_request.messages), max_inline_file_total_bytes=inline_cap)
    opener_prefix = compute_cache_key(opener_api[: opener_request.cache.context_count])
    revise_prefix = compute_cache_key(revise_api[: revise_request.cache.context_count])
    assert opener_prefix == revise_prefix

    # The cache prefix (index 0) carries the opener source, not the follow-up note.
    prefix_text = revise_request.messages[0].content
    assert isinstance(prefix_text, str)
    assert "Source body text." in prefix_text
    assert "NOTE-SENTINEL-9" not in prefix_text

    # The follow-up note IS present, after the cacheable prefix (i.e. threaded later).
    tail_text = "\n".join(
        m.content if isinstance(m.content, str) else ""
        for m in revise_request.messages[revise_request.cache.context_count :]
    )
    assert "NOTE-SENTINEL-9" in tail_text
    # The opener's drafted answer is threaded into the continuation as a prior assistant turn.
    assert any(
        isinstance(m.content, str) and "draft" in m.content
        for m in revise_request.messages
        if m.role.value == "assistant"
    )


# ---------------------------------------------------------------------------
# Import-time lineage validation.
# ---------------------------------------------------------------------------


def test_prompt_result_field_without_continues_is_rejected() -> None:
    with pytest.raises(TypeError, match="without continues="):

        class BadContract(PromptContract[DraftOutput]):
            """Declares a PromptResult input but no continues= lineage."""

            purpose: ClassVar[str] = "p"
            returns: ClassVar[str] = "r"
            success_criteria: ClassVar[str] = "s"

            prior: PromptResult[DraftOutput] = Field(description="prior")


def test_continuation_rejects_predecessor_outside_legal_set() -> None:
    class OtherOutput(FrozenBaseModel):
        """A different output model not produced by the opener."""

        text: str = Field(description="text")

    with pytest.raises(TypeError, match="may only continue outputs from"):

        class MismatchContract(PromptContract[DraftOutput], continues=Continues.once(DraftContract)):
            """The predecessor field type does not match the opener output."""

            purpose: ClassVar[str] = "p"
            returns: ClassVar[str] = "r"
            success_criteria: ClassVar[str] = "s"

            prior: PromptResult[OtherOutput] = Field(description="prior")


def test_repeating_continuation_accepts_self_output() -> None:
    class LoopContract(PromptContract[DraftOutput], continues=Continues.repeating(DraftContract)):
        """A repeating continuation may continue the opener or itself."""

        purpose: ClassVar[str] = "p"
        returns: ClassVar[str] = "r"
        success_criteria: ClassVar[str] = "s"

        prior: PromptResult[DraftOutput] = Field(description="prior")

    assert LoopContract._continues is not None
    assert LoopContract._continues.repeatable is True
