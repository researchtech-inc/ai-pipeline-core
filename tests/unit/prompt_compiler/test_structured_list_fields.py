"""Tests for StructuredField and ListField support in PromptSpec.

StructuredField: BaseModel values rendered as JSON (indent=2) in XML tags.
ListField: list values rendered as individually tagged items in XML wrapper.
Both are sent as user messages before the main prompt, same as MultiLineField.
"""

from unittest.mock import patch

import pytest
from pydantic import BaseModel, Field

from ai_pipeline_core._llm_core.model_response import ModelResponse
from ai_pipeline_core._llm_core.types import TokenUsage
from ai_pipeline_core.documents import Document
from ai_pipeline_core.llm import Conversation
from ai_pipeline_core.prompt_compiler.components import Role
from ai_pipeline_core.prompt_compiler.render import _render_multi_line_messages, render_preview, render_text
from ai_pipeline_core.prompt_compiler.spec import (
    ListField,
    PromptSpec,
    StructuredField,
    _is_list_field,
    _is_structured_field,
)
from tests.support.model_catalog import ALTERNATE_TEST_MODEL


# ---------------------------------------------------------------------------
# Test fixtures / helpers
# ---------------------------------------------------------------------------


class SLDoc(Document):
    """Test document for structured/list tests."""


class SLRole(Role):
    """Test role."""

    text = "experienced analyst"


class Finding(BaseModel):
    """A single finding."""

    model_config = {"frozen": True}

    title: str
    severity: str


class NestedDetail(BaseModel):
    """Nested detail model."""

    model_config = {"frozen": True}

    category: str
    tags: list[str]


def _make_model_response(content: str) -> ModelResponse[str]:
    return ModelResponse[str](
        content=content,
        parsed=content,
        model="test-model",
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


# ---------------------------------------------------------------------------
# StructuredField: import and declaration
# ---------------------------------------------------------------------------


def test_structured_field_importable_from_prompt_compiler() -> None:
    from ai_pipeline_core.prompt_compiler import StructuredField

    assert callable(StructuredField)


def test_structured_field_importable_from_top_level() -> None:
    from ai_pipeline_core import StructuredField

    assert callable(StructuredField)


def test_structured_field_creates_valid_spec() -> None:
    class FindingSpec(PromptSpec):
        """Analyze a finding."""

        input_documents = ()
        role = SLRole
        task = "Analyze the finding provided."

        finding: Finding = StructuredField(description="Primary finding")

    spec = FindingSpec(finding=Finding(title="Oracle attack", severity="critical"))
    assert spec.finding.title == "Oracle attack"


def test_structured_field_with_default() -> None:
    class DefaultStructSpec(PromptSpec):
        """Spec with default structured field."""

        input_documents = ()
        role = SLRole
        task = "Do it"

        finding: Finding = StructuredField(description="Finding", default=Finding(title="default", severity="low"))

    spec = DefaultStructSpec()
    assert spec.finding.title == "default"


def test_structured_field_detection() -> None:
    class DetectSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = SLRole
        task = "Do it"

        finding: Finding = StructuredField(description="Finding")
        name: str = Field(description="Name")

    assert _is_structured_field(DetectSpec.model_fields["finding"]) is True
    assert _is_structured_field(DetectSpec.model_fields["name"]) is False


# ---------------------------------------------------------------------------
# ListField: import and declaration
# ---------------------------------------------------------------------------


def test_list_field_importable_from_prompt_compiler() -> None:
    from ai_pipeline_core.prompt_compiler import ListField

    assert callable(ListField)


def test_list_field_importable_from_top_level() -> None:
    from ai_pipeline_core import ListField

    assert callable(ListField)


def test_list_field_creates_valid_spec_with_base_models() -> None:
    class FindingsSpec(PromptSpec):
        """Analyze findings."""

        input_documents = ()
        role = SLRole
        task = "Review the findings."

        findings: list[Finding] = ListField(description="All findings")

    spec = FindingsSpec(findings=[Finding(title="A", severity="high"), Finding(title="B", severity="low")])
    assert len(spec.findings) == 2


def test_list_field_creates_valid_spec_with_strings() -> None:
    class QueriesSpec(PromptSpec):
        """Evaluate queries."""

        input_documents = ()
        role = SLRole
        task = "Evaluate the queries."

        queries: list[str] = ListField(description="Search queries")

    spec = QueriesSpec(queries=["query one", "query two"])
    assert len(spec.queries) == 2


def test_list_field_detection() -> None:
    class DetectSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = SLRole
        task = "Do it"

        findings: list[Finding] = ListField(description="Findings")
        name: str = Field(description="Name")

    assert _is_list_field(DetectSpec.model_fields["findings"]) is True
    assert _is_list_field(DetectSpec.model_fields["name"]) is False


# ---------------------------------------------------------------------------
# StructuredField: rendering
# ---------------------------------------------------------------------------


def test_structured_field_renders_json_in_xml() -> None:
    class RenderStructSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = SLRole
        task = "Analyze the finding."

        finding: Finding = StructuredField(description="Primary finding")

    spec = RenderStructSpec(finding=Finding(title="Oracle attack", severity="critical"))
    messages = _render_multi_line_messages(spec)

    assert len(messages) == 1
    field_name, xml_block = messages[0]
    assert field_name == "finding"
    assert "<finding>" in xml_block
    assert "</finding>" in xml_block
    assert '"title": "Oracle attack"' in xml_block
    assert '"severity": "critical"' in xml_block


def test_structured_field_context_placeholder() -> None:
    class CtxStructSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = SLRole
        task = "Do it"

        finding: Finding = StructuredField(description="Primary finding")

    rendered = render_text(CtxStructSpec(finding=Finding(title="A", severity="high")))
    assert "**Primary finding:** (provided in <finding> tags in previous message)" in rendered
    # The JSON should NOT be inline
    assert '"title": "A"' not in rendered


def test_structured_field_nested_model() -> None:
    class NestedSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = SLRole
        task = "Do it"

        detail: NestedDetail = StructuredField(description="Detail")

    spec = NestedSpec(detail=NestedDetail(category="infra", tags=["aws", "k8s"]))
    messages = _render_multi_line_messages(spec)
    assert len(messages) == 1
    _, xml_block = messages[0]
    assert '"category": "infra"' in xml_block
    assert '"tags"' in xml_block
    assert '"aws"' in xml_block


# ---------------------------------------------------------------------------
# ListField: rendering (BaseModel items)
# ---------------------------------------------------------------------------


def test_list_field_renders_items_in_xml() -> None:
    class RenderListSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = SLRole
        task = "Review findings."

        findings: list[Finding] = ListField(description="All findings")

    spec = RenderListSpec(
        findings=[
            Finding(title="A", severity="high"),
            Finding(title="B", severity="low"),
            Finding(title="C", severity="medium"),
        ]
    )
    messages = _render_multi_line_messages(spec)

    assert len(messages) == 1
    field_name, xml_block = messages[0]
    assert field_name == "findings"
    assert "<findings>" in xml_block
    assert "</findings>" in xml_block
    assert "<item_1>" in xml_block
    assert "</item_1>" in xml_block
    assert "<item_2>" in xml_block
    assert "<item_3>" in xml_block
    assert '"title": "A"' in xml_block
    assert '"title": "B"' in xml_block
    assert '"title": "C"' in xml_block


def test_list_field_context_placeholder_shows_count() -> None:
    class CountSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = SLRole
        task = "Do it"

        findings: list[Finding] = ListField(description="All findings")

    rendered = render_text(
        CountSpec(
            findings=[
                Finding(title="A", severity="high"),
                Finding(title="B", severity="low"),
            ]
        )
    )
    assert "**All findings:** (2 items provided in <findings> tags in previous message)" in rendered


def test_list_field_single_item() -> None:
    class SingleSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = SLRole
        task = "Do it"

        findings: list[Finding] = ListField(description="Findings")

    spec = SingleSpec(findings=[Finding(title="Only", severity="low")])
    messages = _render_multi_line_messages(spec)
    _, xml_block = messages[0]
    assert "<item_1>" in xml_block
    assert "<item_2>" not in xml_block

    rendered = render_text(spec)
    assert "(1 items provided in <findings> tags in previous message)" in rendered


# ---------------------------------------------------------------------------
# ListField: rendering (string items)
# ---------------------------------------------------------------------------


def test_list_field_string_items() -> None:
    class StringListSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = SLRole
        task = "Evaluate queries."

        queries: list[str] = ListField(description="Search queries")

    spec = StringListSpec(queries=["defi oracle", "flash loan exploit"])
    messages = _render_multi_line_messages(spec)

    assert len(messages) == 1
    _, xml_block = messages[0]
    assert "<queries>" in xml_block
    assert "<item_1>\ndefi oracle\n</item_1>" in xml_block
    assert "<item_2>\nflash loan exploit\n</item_2>" in xml_block


# ---------------------------------------------------------------------------
# ListField: empty list
# ---------------------------------------------------------------------------


def test_list_field_empty_list() -> None:
    class EmptyListSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = SLRole
        task = "Do it"

        items: list[str] = ListField(description="Items", default=[])

    spec = EmptyListSpec()
    messages = _render_multi_line_messages(spec)

    assert len(messages) == 1
    _, xml_block = messages[0]
    assert "<items>" in xml_block
    assert "</items>" in xml_block
    assert "<item_1>" not in xml_block

    rendered = render_text(spec)
    assert "(0 items provided in <items> tags in previous message)" in rendered


# ---------------------------------------------------------------------------
# Mixed fields: coexistence
# ---------------------------------------------------------------------------


@pytest.mark.ai_docs
def test_mixed_field_types_coexist() -> None:
    """All four field types on one PromptSpec: Field, MultiLineField, StructuredField, ListField."""
    from ai_pipeline_core.prompt_compiler import MultiLineField

    class MixedSpec(PromptSpec):
        """Mixed field types."""

        input_documents = ()
        role = SLRole
        task = "Process all inputs."

        name: str = Field(description="Project name")
        feedback: str = MultiLineField(description="Review feedback")
        finding: Finding = StructuredField(description="Primary finding")
        findings: list[Finding] = ListField(description="All findings")

    spec = MixedSpec(
        name="ACME",
        feedback="Great work\non this",
        finding=Finding(title="A", severity="high"),
        findings=[Finding(title="B", severity="low")],
    )

    rendered = render_text(spec)
    # Regular field inlined
    assert "**Project name:**\nACME" in rendered
    # MultiLineField placeholder
    assert "(provided in <feedback> tags in previous message)" in rendered
    # StructuredField placeholder
    assert "(provided in <finding> tags in previous message)" in rendered
    # ListField placeholder with count
    assert "(1 items provided in <findings> tags in previous message)" in rendered

    messages = _render_multi_line_messages(spec)
    field_names = [name for name, _ in messages]
    assert field_names == ["feedback", "finding", "findings"]


def test_mixed_fields_preserve_declaration_order() -> None:
    class OrderSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = SLRole
        task = "Do it"

        alpha: Finding = StructuredField(description="Alpha")
        beta: list[str] = ListField(description="Beta")
        gamma: str = Field(description="Gamma")

    spec = OrderSpec(
        alpha=Finding(title="A", severity="high"),
        beta=["b1", "b2"],
        gamma="short",
    )
    messages = _render_multi_line_messages(spec)
    assert [name for name, _ in messages] == ["alpha", "beta"]


# ---------------------------------------------------------------------------
# Preview rendering
# ---------------------------------------------------------------------------


def test_preview_structured_field() -> None:
    class PreviewStructSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = SLRole
        task = "Analyze it"

        finding: Finding = StructuredField(description="Finding")

    preview = render_preview(PreviewStructSpec)
    assert "<finding>{finding}</finding>" in preview
    assert "---" in preview


def test_preview_list_field() -> None:
    class PreviewListSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = SLRole
        task = "Review items"

        findings: list[Finding] = ListField(description="Findings")

    preview = render_preview(PreviewListSpec)
    assert "<findings>{findings}</findings>" in preview
    assert "---" in preview


def test_preview_no_separator_without_special_fields() -> None:
    class PlainSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = SLRole
        task = "Do it"

        name: str = Field(description="Name")

    preview = render_preview(PlainSpec)
    assert "---" not in preview


# ---------------------------------------------------------------------------
# Conversation.send_spec integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_spec_structured_field_as_user_message() -> None:
    class SendStructSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = SLRole
        task = "Analyze the finding."

        finding: Finding = StructuredField(description="Finding")

    response = _make_model_response("analysis done")
    conv_after = Conversation[str](model=ALTERNATE_TEST_MODEL, messages=(response,))

    sent_content: list[str] = []

    async def fake_send(self, content, *, purpose=None, **kwargs):
        if isinstance(content, str):
            sent_content.append(content)
        return conv_after

    with patch.object(Conversation, "send", fake_send):
        conv = Conversation[str](model=ALTERNATE_TEST_MODEL)
        spec = SendStructSpec(finding=Finding(title="Test", severity="high"))
        await conv.send_spec(spec)

    assert len(sent_content) >= 1
    prompt_text = sent_content[-1]
    # JSON should not be in the prompt text
    assert '"title": "Test"' not in prompt_text
    assert "provided in <finding> tags in previous message" in prompt_text


@pytest.mark.asyncio
async def test_send_spec_list_field_as_user_message() -> None:
    class SendListSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = SLRole
        task = "Review findings."

        findings: list[Finding] = ListField(description="Findings")

    response = _make_model_response("review done")
    messages_at_send: list[tuple[object, ...]] = []

    async def fake_send(self, content, *, purpose=None, **kwargs):
        messages_at_send.append(self.messages)
        return Conversation[str](model=ALTERNATE_TEST_MODEL, messages=(*self.messages, response))

    with patch.object(Conversation, "send", fake_send):
        conv = Conversation[str](model=ALTERNATE_TEST_MODEL)
        spec = SendListSpec(findings=[Finding(title="A", severity="high"), Finding(title="B", severity="low")])
        await conv.send_spec(spec)

    assert len(messages_at_send) == 1
    msgs = messages_at_send[0]
    msg_texts = [m.text for m in msgs if hasattr(m, "text")]
    xml_msgs = [t for t in msg_texts if "<findings>" in t]
    assert len(xml_msgs) == 1
    assert "<item_1>" in xml_msgs[0]
    assert "<item_2>" in xml_msgs[0]


@pytest.mark.asyncio
async def test_send_spec_all_field_types_combined() -> None:
    from ai_pipeline_core.prompt_compiler import MultiLineField

    class CombinedSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = SLRole
        task = "Process everything."

        name: str = Field(description="Name")
        notes: str = MultiLineField(description="Notes")
        finding: Finding = StructuredField(description="Finding")
        findings: list[Finding] = ListField(description="Findings")

    response = _make_model_response("done")
    messages_at_send: list[tuple[object, ...]] = []

    async def fake_send(self, content, *, purpose=None, **kwargs):
        messages_at_send.append(self.messages)
        return Conversation[str](model=ALTERNATE_TEST_MODEL, messages=(*self.messages, response))

    with patch.object(Conversation, "send", fake_send):
        conv = Conversation[str](model=ALTERNATE_TEST_MODEL)
        spec = CombinedSpec(
            name="ACME",
            notes="some notes",
            finding=Finding(title="X", severity="low"),
            findings=[Finding(title="Y", severity="high")],
        )
        await conv.send_spec(spec)

    assert len(messages_at_send) == 1
    msgs = messages_at_send[0]
    msg_texts = [m.text for m in msgs if hasattr(m, "text")]
    # All three special fields combined in a single user message
    combined = [t for t in msg_texts if "<notes>" in t or "<finding>" in t or "<findings>" in t]
    assert len(combined) == 1
    assert "<notes>some notes</notes>" in combined[0]
    assert "<finding>" in combined[0]
    assert "<findings>" in combined[0]
    assert "<item_1>" in combined[0]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_structured_field_requires_description() -> None:
    with pytest.raises(TypeError, match=r"must use Field\(description='\.\.\.'\)|description"):

        class BadSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = SLRole
            task = "Do it"

            finding: Finding = StructuredField()  # type: ignore[call-arg]  # negative test: wrong call signature


def test_list_field_requires_description() -> None:
    with pytest.raises(TypeError, match=r"must use Field\(description='\.\.\.'\)|description"):

        class BadSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = SLRole
            task = "Do it"

            items: list[str] = ListField()  # type: ignore[call-arg]  # negative test: wrong call signature


def test_structured_field_frozen_spec() -> None:
    class FrozenStructSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = SLRole
        task = "Do it"

        finding: Finding = StructuredField(description="Finding")

    spec = FrozenStructSpec(finding=Finding(title="A", severity="high"))
    with pytest.raises(Exception):
        spec.finding = Finding(title="B", severity="low")  # type: ignore[misc]  # frozen model mutation negative test


def test_list_field_with_nested_models() -> None:
    class NestedListSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = SLRole
        task = "Do it"

        details: list[NestedDetail] = ListField(description="Details")

    spec = NestedListSpec(
        details=[
            NestedDetail(category="infra", tags=["aws", "k8s"]),
            NestedDetail(category="app", tags=["python"]),
        ]
    )
    messages = _render_multi_line_messages(spec)
    _, xml_block = messages[0]
    assert "<item_1>" in xml_block
    assert '"category": "infra"' in xml_block
    assert '"tags"' in xml_block
    assert "<item_2>" in xml_block
    assert '"category": "app"' in xml_block


def test_follow_up_spec_with_structured_field() -> None:
    class InitSpec(PromptSpec):
        """Init."""

        input_documents = ()
        role = SLRole
        task = "Start"

    class FollowStructSpec(PromptSpec, follows=InitSpec):
        """Follow with structured."""

        task = "Continue with finding."

        finding: Finding = StructuredField(description="Finding")

    spec = FollowStructSpec(finding=Finding(title="A", severity="high"))
    rendered = render_text(spec)
    assert "# Role" not in rendered
    assert "(provided in <finding> tags in previous message)" in rendered

    messages = _render_multi_line_messages(spec)
    assert len(messages) == 1
    assert messages[0][0] == "finding"


def test_follow_up_spec_with_list_field() -> None:
    class InitSpec(PromptSpec):
        """Init."""

        input_documents = ()
        role = SLRole
        task = "Start"

    class FollowListSpec(PromptSpec, follows=InitSpec):
        """Follow with list."""

        task = "Continue with findings."

        findings: list[Finding] = ListField(description="Findings")

    spec = FollowListSpec(findings=[Finding(title="A", severity="high")])
    rendered = render_text(spec)
    assert "(1 items provided in <findings> tags in previous message)" in rendered


def test_structured_field_json_is_indented() -> None:
    """Verify the JSON output uses indent=2 for readability."""

    class IndentSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = SLRole
        task = "Do it"

        finding: Finding = StructuredField(description="Finding")

    spec = IndentSpec(finding=Finding(title="Test", severity="high"))
    messages = _render_multi_line_messages(spec)
    _, xml_block = messages[0]
    # indent=2 means fields should be on separate lines with 2-space indent
    assert '  "title": "Test"' in xml_block
    assert '  "severity": "high"' in xml_block


def test_list_field_basemodel_items_json_is_indented() -> None:
    """Verify BaseModel items in ListField use indent=2 JSON."""

    class IndentListSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = SLRole
        task = "Do it"

        findings: list[Finding] = ListField(description="Findings")

    spec = IndentListSpec(findings=[Finding(title="Test", severity="high")])
    messages = _render_multi_line_messages(spec)
    _, xml_block = messages[0]
    assert '  "title": "Test"' in xml_block
