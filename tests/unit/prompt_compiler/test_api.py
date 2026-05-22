"""Tests for send_spec(), stop sequences, and Conversation.send_spec()."""

from unittest.mock import patch

import pytest
from pydantic import BaseModel

from ai_pipeline_core._llm_core.model_response import ModelResponse
from ai_pipeline_core._llm_core.types import TokenUsage
from ai_pipeline_core.documents import Document
from ai_pipeline_core.llm import Conversation
from ai_pipeline_core.prompt_compiler.components import Role
from ai_pipeline_core.prompt_compiler.spec import PromptSpec
from tests.support.model_catalog import DEFAULT_TEST_MODEL, ALTERNATE_TEST_MODEL


# ---------------------------------------------------------------------------
# Test fixtures / helpers
# ---------------------------------------------------------------------------


class ApiDoc(Document):
    """API input document."""


class ApiRole(Role):
    """API role."""

    text = "careful evaluator"


class StructuredResponse(BaseModel):
    """Structured output model."""

    answer: str


class PlainSpec(PromptSpec):
    """Plain text output spec."""

    input_documents = ()
    role = ApiRole
    task = "Respond briefly"


class XmlSpec(PromptSpec):
    """XML-wrapped text output spec (via output_structure)."""

    input_documents = ()
    role = ApiRole
    task = "Respond in wrapped text"
    output_structure = "## Summary"


class StructuredSpec(PromptSpec[StructuredResponse]):
    """Structured output spec."""

    input_documents = ()
    role = ApiRole
    task = "Return structured data"


class SpecWithInputDocs(PromptSpec):
    """Spec with input_documents declared."""

    input_documents = (ApiDoc,)
    role = ApiRole
    task = "Analyze the document"


# ---------------------------------------------------------------------------
# AIModel stop sequence capability
# ---------------------------------------------------------------------------


def test_supports_stop_sequence_is_explicit_aimodel_capability() -> None:
    assert DEFAULT_TEST_MODEL.supports_stop_sequences is True
    assert ALTERNATE_TEST_MODEL.model_copy(update={"supports_stop_sequences": False}).supports_stop_sequences is False


# ---------------------------------------------------------------------------
# Conversation.send_spec: helpers
# ---------------------------------------------------------------------------


def _make_model_response(content: str) -> ModelResponse[str]:
    return ModelResponse[str](
        content=content,
        parsed=content,
        model="test-model",
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


# ---------------------------------------------------------------------------
# Conversation.send_spec: auto-extraction for output_structure specs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_spec_xml_auto_extracts_result() -> None:
    """output_structure spec auto-extracts <result> tags — conv.content returns clean text."""
    raw_content = "preamble <result>\n  extracted content \n</result> trailing"
    response = _make_model_response(raw_content)

    conv_after_send = Conversation[str](model=ALTERNATE_TEST_MODEL, messages=(response,))

    async def fake_send(self, prompt_text, *, purpose, **kwargs):
        return conv_after_send

    with (
        patch.object(Conversation, "send", fake_send),
        patch("ai_pipeline_core.llm.conversation.render_text", return_value="RENDERED"),
    ):
        conv = Conversation[str](model=ALTERNATE_TEST_MODEL)
        result = await conv.send_spec(XmlSpec())

    assert result.content == "extracted content"


@pytest.mark.asyncio
async def test_send_spec_plain_preserves_raw_response() -> None:
    """Spec without output_structure preserves raw response content."""
    raw_content = "plain response text"
    response = _make_model_response(raw_content)

    conv_after_send = Conversation[str](model=ALTERNATE_TEST_MODEL, messages=(response,))

    async def fake_send(self, prompt_text, *, purpose, **kwargs):
        return conv_after_send

    with (
        patch.object(Conversation, "send", fake_send),
        patch("ai_pipeline_core.llm.conversation.render_text", return_value="RENDERED"),
    ):
        conv = Conversation[str](model=ALTERNATE_TEST_MODEL)
        result = await conv.send_spec(PlainSpec())

    assert result.content == raw_content


# ---------------------------------------------------------------------------
# Conversation.send_spec: input_documents warning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_spec_logs_missing_documents(caplog: pytest.LogCaptureFixture) -> None:
    """Warning logged when spec declares input_documents but no documents passed."""
    response = _make_model_response("response")
    conv_after_send = Conversation[str](model=ALTERNATE_TEST_MODEL, messages=(response,))

    async def fake_send(self, prompt_text, *, purpose, **kwargs):
        return conv_after_send

    with (
        patch.object(Conversation, "send", fake_send),
        patch("ai_pipeline_core.llm.conversation.render_text", return_value="RENDERED"),
    ):
        conv = Conversation[str](model=ALTERNATE_TEST_MODEL)
        with caplog.at_level("WARNING"):
            await conv.send_spec(SpecWithInputDocs())

    assert any("declares input_documents" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_send_spec_no_warning_when_documents_provided(caplog: pytest.LogCaptureFixture) -> None:
    """No warning when documents are actually provided."""
    doc = ApiDoc(name="test.txt", content=b"data")
    response = _make_model_response("response")
    conv_after_send = Conversation[str](model=ALTERNATE_TEST_MODEL, messages=(response,))

    async def fake_send(self, prompt_text, *, purpose, **kwargs):
        return conv_after_send

    with (
        patch.object(Conversation, "send", fake_send),
        patch("ai_pipeline_core.llm.conversation.render_text", return_value="RENDERED"),
    ):
        conv = Conversation[str](model=ALTERNATE_TEST_MODEL)
        with caplog.at_level("WARNING"):
            await conv.send_spec(SpecWithInputDocs(), documents=[doc])

    assert not any("declares input_documents" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Conversation.send_spec: follow-up specs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_spec_follow_up_no_warning_for_missing_docs(caplog: pytest.LogCaptureFixture) -> None:
    """Follow-up specs do not warn about missing documents."""

    class FollowSpec(PromptSpec, follows=PlainSpec):
        """Follow-up."""

        task = "Continue analysis"

    response = _make_model_response("follow-up response")
    conv_after_send = Conversation[str](model=ALTERNATE_TEST_MODEL, messages=(response,))

    async def fake_send(self, prompt_text, *, purpose, **kwargs):
        return conv_after_send

    with (
        patch.object(Conversation, "send", fake_send),
        patch("ai_pipeline_core.llm.conversation.render_text", return_value="RENDERED"),
    ):
        conv = Conversation[str](model=ALTERNATE_TEST_MODEL)
        with caplog.at_level("WARNING"):
            await conv.send_spec(FollowSpec())

    assert not any("declares input_documents" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_send_spec_follow_up_documents_in_messages() -> None:
    """Follow-up specs route documents via with_documents() (messages), not with_context()."""

    class FollowWithDocs(PromptSpec, follows=SpecWithInputDocs):
        """Follow-up that adds docs."""

        input_documents = (ApiDoc,)
        task = "Continue with new evidence"

    doc = ApiDoc(name="extra.txt", content=b"extra data")
    response = _make_model_response("follow-up with docs")
    conv_after_send = Conversation[str](model=ALTERNATE_TEST_MODEL, messages=(response,))

    calls: dict[str, int] = {"with_documents": 0, "with_context": 0}
    original_with_documents = Conversation.with_documents
    original_with_context = Conversation.with_context

    def tracking_with_documents(self, docs):
        calls["with_documents"] += 1
        return original_with_documents(self, docs)

    def tracking_with_context(self, *docs):
        calls["with_context"] += 1
        return original_with_context(self, *docs)

    async def fake_send(self, prompt_text, *, purpose, **kwargs):
        return conv_after_send

    with (
        patch.object(Conversation, "send", fake_send),
        patch.object(Conversation, "with_documents", tracking_with_documents),
        patch.object(Conversation, "with_context", tracking_with_context),
        patch("ai_pipeline_core.llm.conversation.render_text", return_value="RENDERED"),
    ):
        conv = Conversation[str](model=ALTERNATE_TEST_MODEL)
        await conv.send_spec(FollowWithDocs(), documents=[doc])

    assert calls["with_documents"] == 1, "Follow-up should use with_documents()"
    assert calls["with_context"] == 0, "Follow-up should NOT use with_context()"
