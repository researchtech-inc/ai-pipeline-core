"""Tests for MultiLineField support in PromptSpec.

Multi-line fields are declared via MultiLineField(description=...) and are:
- Combined into a single XML-tagged user message sent BEFORE the main prompt (not inlined)
- Rendered with a --- separator in preview/compile output
- Referenced in the prompt Context section as "(provided in <tag> tags in previous message)"
- Regular (non-multi-line) fields that contain newlines or exceed 500 chars are auto-promoted
  to multi-line treatment with a warning
"""

from unittest.mock import patch

import pytest
from pydantic import Field

from ai_pipeline_core._llm_core.model_response import ModelResponse
from ai_pipeline_core._llm_core.types import TokenUsage
from ai_pipeline_core.documents import Document
from ai_pipeline_core.llm import Conversation
from ai_pipeline_core.prompt_compiler.components import Role
from ai_pipeline_core.prompt_compiler.render import _MAX_FIELD_VALUE_LENGTH, render_preview, render_text
from ai_pipeline_core.prompt_compiler.spec import PromptSpec
from tests.support.model_catalog import ALTERNATE_TEST_MODEL


# ---------------------------------------------------------------------------
# Test fixtures / helpers
# ---------------------------------------------------------------------------


class MlDoc(Document):
    """Multi-line test document."""


class MlRole(Role):
    """Multi-line test role."""

    text = "experienced reviewer"


def _make_model_response(content: str) -> ModelResponse[str]:
    return ModelResponse[str](
        content=content,
        parsed=content,
        model="test-model",
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


# ---------------------------------------------------------------------------
# MultiLineField: import and declaration
# ---------------------------------------------------------------------------


def test_multi_line_field_importable_from_prompt_compiler() -> None:
    """MultiLineField is exported from prompt_compiler package."""
    from ai_pipeline_core.prompt_compiler import MultiLineField

    assert callable(MultiLineField)


def test_multi_line_field_importable_from_top_level() -> None:
    """MultiLineField is exported from top-level package."""
    from ai_pipeline_core import MultiLineField

    assert callable(MultiLineField)


def test_multi_line_field_creates_valid_spec() -> None:
    """PromptSpec with MultiLineField passes definition-time validation."""
    from ai_pipeline_core.prompt_compiler import MultiLineField

    class ReviewSpec(PromptSpec):
        """Analyze a review."""

        input_documents = ()
        role = MlRole
        task = "Analyze the review provided."

        review: str = MultiLineField(description="Review text to analyze")

    spec = ReviewSpec(review="First line\nSecond line")
    assert spec.review == "First line\nSecond line"


def test_multi_line_field_with_default_value() -> None:
    """MultiLineField supports default values."""
    from ai_pipeline_core.prompt_compiler import MultiLineField

    class DefaultSpec(PromptSpec):
        """Spec with default multi-line field."""

        input_documents = ()
        role = MlRole
        task = "Do it"

        notes: str = MultiLineField(description="Additional notes", default="")

    spec = DefaultSpec()
    assert spec.notes == ""


def test_multi_line_field_requires_description() -> None:
    """MultiLineField without description raises TypeError at definition time."""
    from ai_pipeline_core.prompt_compiler import MultiLineField

    with pytest.raises(TypeError, match=r"must use Field\(description='\.\.\.'\)|description"):

        class BadSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = MlRole
            task = "Do it"

            review: str = MultiLineField()  # type: ignore[call-arg]


def test_multi_line_field_coexists_with_regular_fields() -> None:
    """Spec can have both regular Field and MultiLineField."""
    from ai_pipeline_core.prompt_compiler import MultiLineField

    class MixedSpec(PromptSpec):
        """Mixed fields spec."""

        input_documents = ()
        role = MlRole
        task = "Analyze the review for the given project."

        project_name: str = Field(description="Project name")
        review: str = MultiLineField(description="Review text")

    spec = MixedSpec(project_name="ACME", review="Great work\non this project")
    assert spec.project_name == "ACME"
    assert spec.review == "Great work\non this project"


# ---------------------------------------------------------------------------
# MultiLineField: detection helper
# ---------------------------------------------------------------------------


def test_is_multi_line_field_detection() -> None:
    """is_multi_line_field correctly identifies multi-line fields."""
    from ai_pipeline_core.prompt_compiler import MultiLineField
    from ai_pipeline_core.prompt_compiler.spec import _is_multi_line_field as is_multi_line_field

    class DetectSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = MlRole
        task = "Do it"

        regular: str = Field(description="Regular field")
        review: str = MultiLineField(description="Review text")

    assert is_multi_line_field(DetectSpec.model_fields["review"]) is True
    assert is_multi_line_field(DetectSpec.model_fields["regular"]) is False


# ---------------------------------------------------------------------------
# Rendering: multi-line fields excluded from prompt text
# ---------------------------------------------------------------------------


def test_render_text_excludes_multi_line_fields_from_context() -> None:
    """Multi-line fields do NOT appear inline in the rendered prompt Context section."""
    from ai_pipeline_core.prompt_compiler import MultiLineField

    class RenderSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = MlRole
        task = "Analyze the review."

        project: str = Field(description="Project name")
        review: str = MultiLineField(description="Review text")

    rendered = render_text(RenderSpec(project="ACME", review="Line 1\nLine 2"))
    # Regular field is inline
    assert "**Project name:**\nACME" in rendered
    # Multi-line field value is NOT inline
    assert "Line 1\nLine 2" not in rendered
    # But a reference placeholder IS present
    assert "Review text" in rendered
    assert "<review>" in rendered


def test_render_text_multi_line_reference_in_context() -> None:
    """Multi-line field shows a reference like '(provided in <tag> tags in previous message)' in Context."""
    from ai_pipeline_core.prompt_compiler import MultiLineField

    class RefSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = MlRole
        task = "Process review."

        review: str = MultiLineField(description="Review feedback")

    rendered = render_text(RefSpec(review="some review text"))
    assert "**Review feedback:** (provided in <review> tags in previous message)" in rendered


def test_render_text_multi_line_field_only_no_regular_fields() -> None:
    """Context section renders when only multi-line fields exist (no regular fields)."""
    from ai_pipeline_core.prompt_compiler import MultiLineField

    class OnlyMlSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = MlRole
        task = "Do it"

        body: str = MultiLineField(description="Body content")

    rendered = render_text(OnlyMlSpec(body="content here"))
    assert "# Context" in rendered
    assert "<body>" in rendered


# ---------------------------------------------------------------------------
# Rendering: _render_multi_line_messages
# ---------------------------------------------------------------------------


def test__render_multi_line_messages_returns_xml_blocks() -> None:
    """_render_multi_line_messages returns list of (field_name, xml_string) pairs."""
    from ai_pipeline_core.prompt_compiler import MultiLineField
    from ai_pipeline_core.prompt_compiler.render import _render_multi_line_messages

    class MsgSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = MlRole
        task = "Do it"

        review: str = MultiLineField(description="Review text")
        notes: str = MultiLineField(description="Additional notes")
        project: str = Field(description="Project name")

    spec = MsgSpec(review="review content", notes="note content", project="ACME")
    messages = _render_multi_line_messages(spec)

    assert len(messages) == 2
    field_names = [name for name, _ in messages]
    assert "review" in field_names
    assert "notes" in field_names
    assert "project" not in field_names

    for field_name, xml_block in messages:
        assert xml_block == f"<{field_name}>{getattr(spec, field_name)}</{field_name}>"


def test__render_multi_line_messages_empty_when_no_multi_line_fields() -> None:
    """_render_multi_line_messages returns empty list when no multi-line fields."""
    from ai_pipeline_core.prompt_compiler.render import _render_multi_line_messages

    class NoMlSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = MlRole
        task = "Do it"

        item: str = Field(description="Item")

    messages = _render_multi_line_messages(NoMlSpec(item="x"))
    assert messages == []


def test__render_multi_line_messages_preserves_field_order() -> None:
    """Multi-line messages are returned in field declaration order."""
    from ai_pipeline_core.prompt_compiler import MultiLineField
    from ai_pipeline_core.prompt_compiler.render import _render_multi_line_messages

    class OrderSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = MlRole
        task = "Do it"

        first: str = MultiLineField(description="First")
        second: str = MultiLineField(description="Second")
        third: str = MultiLineField(description="Third")

    spec = OrderSpec(first="a", second="b", third="c")
    messages = _render_multi_line_messages(spec)
    assert [name for name, _ in messages] == ["first", "second", "third"]


# ---------------------------------------------------------------------------
# Rendering: preview with multi-line fields
# ---------------------------------------------------------------------------


def test_render_preview_shows_multi_line_blocks_with_separator() -> None:
    """render_preview shows multi-line XML blocks, then ---, then the prompt."""
    from ai_pipeline_core.prompt_compiler import MultiLineField

    class PreviewSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = MlRole
        task = "Analyze it"

        review: str = MultiLineField(description="Review text")
        project: str = Field(description="Project name")

    preview = render_preview(PreviewSpec)
    # Multi-line block appears before separator
    assert "<review>{review}</review>" in preview
    assert "---" in preview
    # Main prompt appears after separator
    parts = preview.split("---")
    assert len(parts) == 2
    assert "<review>" in parts[0]
    assert "# Role" in parts[1]
    # Regular field is in the main prompt part
    assert "**Project name:**" in parts[1]


def test_render_preview_no_separator_when_no_multi_line_fields() -> None:
    """render_preview has no --- separator when there are no multi-line fields."""

    class PlainSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = MlRole
        task = "Do it"

        item: str = Field(description="Item")

    preview = render_preview(PlainSpec)
    assert "---" not in preview


# ---------------------------------------------------------------------------
# Regular field: auto-promotion for long/multiline values (regression)
# ---------------------------------------------------------------------------


def test_regular_field_multiline_value_auto_promoted() -> None:
    """Regular Field with multiline value is auto-promoted to multi-line treatment (no ValueError)."""

    class AutoSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = MlRole
        task = "Do it"

        feedback: str = Field(description="Feedback")

    rendered = render_text(AutoSpec(feedback="line1\nline2"))
    assert "line1\nline2" not in rendered
    assert "(provided in <feedback> tags in previous message)" in rendered


def test_regular_field_long_value_auto_promoted() -> None:
    """Regular Field with value exceeding _MAX_FIELD_VALUE_LENGTH is auto-promoted (no ValueError)."""

    class AutoSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = MlRole
        task = "Do it"

        feedback: str = Field(description="Feedback")

    long_value = "x" * (_MAX_FIELD_VALUE_LENGTH + 1)
    rendered = render_text(AutoSpec(feedback=long_value))
    assert long_value not in rendered
    assert "(provided in <feedback> tags in previous message)" in rendered


def test_regular_field_auto_promoted_included_in_multi_line_messages() -> None:
    """Auto-promoted regular fields are included in _render_multi_line_messages output."""
    from ai_pipeline_core.prompt_compiler.render import _render_multi_line_messages

    class AutoSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = MlRole
        task = "Do it"

        feedback: str = Field(description="Feedback")
        item: str = Field(description="Item")

    long_value = "x" * (_MAX_FIELD_VALUE_LENGTH + 1)
    spec = AutoSpec(feedback=long_value, item="short")
    messages = _render_multi_line_messages(spec)
    field_names = [name for name, _ in messages]
    assert "feedback" in field_names
    assert "item" not in field_names


def test_regular_field_auto_promoted_warning_logged(caplog: pytest.LogCaptureFixture) -> None:
    """Auto-promoted regular fields emit a warning."""
    import logging

    class WarnSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = MlRole
        task = "Do it"

        feedback: str = Field(description="Feedback")

    with caplog.at_level(logging.WARNING):
        render_text(WarnSpec(feedback="line1\nline2"))

    assert len(caplog.records) == 1
    assert "feedback" in caplog.records[0].message
    assert "WarnSpec" in caplog.records[0].message
    assert "MultiLineField" in caplog.records[0].message


def test_regular_field_short_value_no_error() -> None:
    """Regular Field with short single-line value renders normally."""

    class OkSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = MlRole
        task = "Do it"

        item: str = Field(description="Item")

    rendered = render_text(OkSpec(item="short value"))
    assert "**Item:**\nshort value" in rendered


def test_regular_field_at_limit_no_error() -> None:
    """Regular Field with value exactly at _MAX_FIELD_VALUE_LENGTH is fine."""

    class ExactSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = MlRole
        task = "Do it"

        text: str = Field(description="Some text")

    exact_value = "x" * _MAX_FIELD_VALUE_LENGTH
    rendered = render_text(ExactSpec(text=exact_value))
    assert f"**Some text:**\n{exact_value}" in rendered


# ---------------------------------------------------------------------------
# Conversation.send_spec: multi-line fields as messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_spec_adds_multi_line_fields_as_user_messages() -> None:
    """send_spec places multi-line field values as separate user messages before the prompt."""
    from ai_pipeline_core.prompt_compiler import MultiLineField

    class SendMlSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = MlRole
        task = "Analyze the review."

        review: str = MultiLineField(description="Review text")

    response = _make_model_response("analysis result")
    conv_after_send = Conversation[str](model=ALTERNATE_TEST_MODEL, messages=(response,))

    sent_content: list[str] = []

    async def fake_send(self, content, *, purpose=None, **kwargs):
        if isinstance(content, str):
            sent_content.append(content)
        return conv_after_send

    with patch.object(Conversation, "send", fake_send):
        conv = Conversation[str](model=ALTERNATE_TEST_MODEL)
        spec = SendMlSpec(review="This is the review\nwith multiple lines")
        await conv.send_spec(spec)

    # The multi-line field XML should have been added as a message
    # The main prompt should NOT contain the review text inline
    assert len(sent_content) >= 1
    prompt_text = sent_content[-1]  # Last send call is the main prompt
    assert "This is the review" not in prompt_text
    assert "<review>" in prompt_text or "provided in <review> tags in previous message" in prompt_text


@pytest.mark.asyncio
async def test_send_spec_multi_line_fields_in_single_message() -> None:
    """All multi-line fields are combined into a single user message before the prompt."""
    from ai_pipeline_core.prompt_compiler import MultiLineField

    class OrderMlSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = MlRole
        task = "Process inputs."

        review: str = MultiLineField(description="Review")
        notes: str = MultiLineField(description="Notes")

    response = _make_model_response("done")
    messages_at_send: list[tuple[object, ...]] = []

    async def fake_send(self, content, *, purpose=None, **kwargs):
        messages_at_send.append(self.messages)
        return Conversation[str](model=ALTERNATE_TEST_MODEL, messages=(*self.messages, response))

    with patch.object(Conversation, "send", fake_send):
        conv = Conversation[str](model=ALTERNATE_TEST_MODEL)
        spec = OrderMlSpec(review="review text", notes="note text")
        await conv.send_spec(spec)

    # At the time send() is called, the conversation should have exactly one XML message
    assert len(messages_at_send) == 1
    msgs = messages_at_send[0]
    msg_texts = [m.text for m in msgs if hasattr(m, "text")]
    xml_msgs = [t for t in msg_texts if "<review>" in t or "<notes>" in t]
    assert len(xml_msgs) == 1, "All multi-line fields must be in a single user message"
    assert "<review>review text</review>" in xml_msgs[0]
    assert "<notes>note text</notes>" in xml_msgs[0]


@pytest.mark.asyncio
async def test_send_spec_multi_line_with_documents() -> None:
    """Multi-line fields and documents coexist — documents in context, multi-line in messages."""
    from ai_pipeline_core.prompt_compiler import MultiLineField

    class DocMlSpec(PromptSpec):
        """Doc."""

        input_documents = (MlDoc,)
        role = MlRole
        task = "Analyze with context."

        review: str = MultiLineField(description="Review text")

    response = _make_model_response("done")
    context_at_send: list[tuple[object, ...]] = []
    messages_at_send: list[tuple[object, ...]] = []

    async def fake_send(self, content, *, purpose=None, **kwargs):
        context_at_send.append(self.context)
        messages_at_send.append(self.messages)
        return Conversation[str](model=ALTERNATE_TEST_MODEL, messages=(*self.messages, response))

    doc = MlDoc(name="source.txt", content=b"source data")
    with patch.object(Conversation, "send", fake_send):
        conv = Conversation[str](model=ALTERNATE_TEST_MODEL)
        spec = DocMlSpec(review="review content")
        await conv.send_spec(spec, documents=[doc])

    # Document should be in context
    assert len(context_at_send) == 1
    assert any(isinstance(item, Document) for item in context_at_send[0])

    # Multi-line field should be in messages (as user message, not document)
    assert len(messages_at_send) == 1
    msg_texts = [m.text for m in messages_at_send[0] if hasattr(m, "text")]
    assert any("<review>" in t for t in msg_texts)


@pytest.mark.asyncio
async def test_send_spec_follow_up_multi_line_in_messages() -> None:
    """Follow-up spec multi-line fields also go to messages."""
    from ai_pipeline_core.prompt_compiler import MultiLineField

    class InitSpec(PromptSpec):
        """Init."""

        input_documents = ()
        role = MlRole
        task = "Start"

    class FollowMlSpec(PromptSpec, follows=InitSpec):
        """Follow with multi-line."""

        task = "Continue with feedback."

        feedback: str = MultiLineField(description="User feedback")

    response = _make_model_response("done")
    messages_at_send: list[tuple[object, ...]] = []

    async def fake_send(self, content, *, purpose=None, **kwargs):
        messages_at_send.append(self.messages)
        return Conversation[str](model=ALTERNATE_TEST_MODEL, messages=(*self.messages, response))

    with patch.object(Conversation, "send", fake_send):
        conv = Conversation[str](model=ALTERNATE_TEST_MODEL)
        spec = FollowMlSpec(feedback="Please fix\nthe issues")
        await conv.send_spec(spec)

    assert len(messages_at_send) == 1
    msg_texts = [m.text for m in messages_at_send[0] if hasattr(m, "text")]
    assert any("<feedback>" in t for t in msg_texts)


# ---------------------------------------------------------------------------
# Token count: multi-line fields included
# ---------------------------------------------------------------------------


def test_approximate_tokens_includes_multi_line_messages() -> None:
    """approximate_tokens_count accounts for multi-line field messages in the conversation."""
    from ai_pipeline_core.llm.conversation import UserMessage

    long_text = "x" * 4000  # ~1000 tokens at 4 chars/token
    xml_msg = f"<review>{long_text}</review>"
    conv = Conversation[str](
        model=ALTERNATE_TEST_MODEL,
        messages=(UserMessage(xml_msg),),
    )
    # The token count should include the XML message
    assert conv.approximate_tokens_count >= 1000
