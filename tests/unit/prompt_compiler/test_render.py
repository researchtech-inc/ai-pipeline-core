"""Tests for prompt_compiler.render (rendering logic)."""

import sys
from collections.abc import Generator
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from pydantic import Field

from ai_pipeline_core.documents import Document
from ai_pipeline_core.prompt_compiler.components import Guide, OutputRule, Role, Rule
from ai_pipeline_core.prompt_compiler.render import (
    _MAX_FIELD_VALUE_LENGTH,
    _RESULT_OPEN,
    _format_numbered_rule,
    _pascal_to_title,
    _render_documents_actual,
    _render_documents_preview,
    _role_sentence,
    render_preview,
    render_text,
)
from ai_pipeline_core.prompt_compiler.spec import PromptSpec


# ---------------------------------------------------------------------------
# Test fixtures / helpers
# ---------------------------------------------------------------------------


class RenderDoc(Document):
    """Primary source document.
    Additional details on second line.
    """


class RenderDocNoDoc(Document):
    pass


class RenderRole(Role):
    """Render role."""

    text = "expert editor"


class VowelRole(Role):
    """Vowel role."""

    text = "AI specialist"


class RenderRule(Rule):
    """Render rule."""

    text = "First line\nSecond line"


class SingleLineRule(Rule):
    """Single line rule."""

    text = "A single line rule"


class RenderOutputRule(OutputRule):
    """Render output rule."""

    text = "Use bullet list format"


@pytest.fixture
def temp_modules() -> Generator[list[str]]:
    created: list[str] = []
    yield created
    for module_name in created:
        sys.modules.pop(module_name, None)


def _make_guide(tmp_path: Path, temp_modules: list[str], *, class_name: str, content: str) -> type[Guide]:
    module_name = f"render_guide_mod_{uuid4().hex}"
    module = ModuleType(module_name)
    module.__file__ = str(tmp_path / f"{module_name}.py")
    sys.modules[module_name] = module
    temp_modules.append(module_name)

    file_path = tmp_path / f"{class_name.lower()}.txt"
    file_path.write_text(content, encoding="utf-8")
    return type(class_name, (Guide,), {"__module__": module_name, "__doc__": "Guide doc.", "template": file_path.name})


# ---------------------------------------------------------------------------
# _role_sentence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role_text", "expected"),
    [
        ("expert editor", "You are an expert editor."),
        ("senior reviewer", "You are a senior reviewer."),
        ("AI specialist", "You are an AI specialist."),
        ("uber analyst", "You are an uber analyst."),
        ("Engineer", "You are an Engineer."),
        ("Overhead manager", "You are an Overhead manager."),
    ],
)
def test_role_sentence_article_selection(role_text: str, expected: str) -> None:
    assert _role_sentence(role_text) == expected


# ---------------------------------------------------------------------------
# _pascal_to_title
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("RiskAssessmentFramework", "Risk Assessment Framework"),
        ("XMLParser", "XML Parser"),
        ("HTTPRequestXML", "HTTP Request XML"),
        ("MyURLParser", "My URL Parser"),
        ("A", "A"),
        ("SimpleClass", "Simple Class"),
    ],
)
def test_pascal_to_title(name: str, expected: str) -> None:
    assert _pascal_to_title(name) == expected


# ---------------------------------------------------------------------------
# _format_numbered_rule
# ---------------------------------------------------------------------------


def test_format_numbered_rule_single_line() -> None:
    assert _format_numbered_rule(3, "Single line") == "3. Single line"


def test_format_numbered_rule_multiline() -> None:
    result = _format_numbered_rule(1, "Line one\nLine two\nLine three")
    assert result == "1. Line one\n   Line two\n   Line three"


# ---------------------------------------------------------------------------
# _render_documents_preview
# ---------------------------------------------------------------------------


def test_render_documents_preview_uses_first_docstring_line() -> None:
    class PreviewSpec(PromptSpec):
        """Doc."""

        input_documents = (RenderDoc,)
        role = RenderRole
        task = "Task"

    text = _render_documents_preview(PreviewSpec)
    assert "RenderDoc\n  Primary source document." in text


def test_render_documents_preview_fallback_for_no_docstring() -> None:
    class PreviewSpec(PromptSpec):
        """Doc."""

        input_documents = (RenderDocNoDoc,)
        role = RenderRole
        task = "Task"

    text = _render_documents_preview(PreviewSpec)
    assert "RenderDocNoDoc\n  No description" in text


# ---------------------------------------------------------------------------
# _render_documents_actual
# ---------------------------------------------------------------------------


def test_render_documents_actual_with_description() -> None:
    doc = RenderDoc(name="source.md", content=b"content", description="Line one\nLine two")
    text = _render_documents_actual([doc])
    assert f"[{doc.id}] source.md" in text
    assert "  Line one\n  Line two" in text


def test_render_documents_actual_without_description() -> None:
    doc = RenderDoc(name="raw.txt", content=b"raw")
    text = _render_documents_actual([doc])
    assert f"[{doc.id}] raw.txt" in text
    # No indented description lines
    lines = text.split(f"[{doc.id}] raw.txt")[1]
    assert lines.strip() == ""


def test_render_documents_actual_multiple_documents() -> None:
    doc1 = RenderDoc(name="a.txt", content=b"a", description="Doc A")
    doc2 = RenderDoc(name="b.txt", content=b"b", description="Doc B")
    text = _render_documents_actual([doc1, doc2])
    assert f"[{doc1.id}] a.txt" in text
    assert f"[{doc2.id}] b.txt" in text


# ---------------------------------------------------------------------------
# render_text: section structure
# ---------------------------------------------------------------------------


def test_render_text_role_section() -> None:
    class MinSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = RenderRole
        task = "Task"

    rendered = render_text(MinSpec())
    assert "# Role\n\nYou are an expert editor." in rendered


def test_render_text_vowel_role_article() -> None:
    class VowelSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = VowelRole
        task = "Task"

    rendered = render_text(VowelSpec())
    assert "You are an AI specialist." in rendered


def test_render_text_task_section() -> None:
    class TaskSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = RenderRole
        task = "Do the thing"

    rendered = render_text(TaskSpec())
    assert "# Task\n\nDo the thing" in rendered


def test_render_text_context_with_preview_documents() -> None:
    class DocSpec(PromptSpec):
        """Doc."""

        input_documents = (RenderDoc,)
        role = RenderRole
        task = "Task"

    rendered = render_text(DocSpec())
    assert "# Context" in rendered
    assert "Documents provided in context:" in rendered
    assert "RenderDoc\n  Primary source document." in rendered


def test_render_text_context_with_actual_documents() -> None:
    class DocSpec(PromptSpec):
        """Doc."""

        input_documents = (RenderDoc,)
        role = RenderRole
        task = "Task"

    doc = RenderDoc(name="input.md", content=b"hello", description="Runtime desc")
    rendered = render_text(DocSpec(), documents=[doc])
    assert f"[{doc.id}] input.md" in rendered
    assert "  Runtime desc" in rendered
    # Should NOT contain preview listing
    assert "RenderDoc\n  Primary source document." not in rendered


def test_render_text_context_with_dynamic_fields() -> None:
    class FieldSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = RenderRole
        task = "Task"

        item: str = Field(description="The item to analyze")

    rendered = render_text(FieldSpec(item="Liquidity risk"))
    assert "# Context" in rendered
    assert "**The item to analyze:**\nLiquidity risk" in rendered


def test_render_text_include_input_documents_false() -> None:
    class DocFieldSpec(PromptSpec):
        """Doc."""

        input_documents = (RenderDoc,)
        role = RenderRole
        task = "Task"

        item: str = Field(description="Item")

    rendered = render_text(DocFieldSpec(item="x"), include_input_documents=False)
    assert "Documents provided in context:" not in rendered
    assert "**Item:**\nx" in rendered


def test_render_text_no_context_section_when_empty() -> None:
    class EmptyContextSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = RenderRole
        task = "Task"

    rendered = render_text(EmptyContextSpec(), include_input_documents=False)
    assert "# Context" not in rendered
    assert "# Task" in rendered


def test_render_text_rules_section() -> None:
    class RulesSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = RenderRole
        task = "Task"

        rules = (SingleLineRule, RenderRule)

    rendered = render_text(RulesSpec())
    assert "# Rules" in rendered
    assert "1. A single line rule" in rendered
    assert "2. First line\n   Second line" in rendered


def test_render_text_no_rules_section_when_empty() -> None:
    class NoRulesSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = RenderRole
        task = "Task"

    rendered = render_text(NoRulesSpec())
    assert "# Rules" not in rendered


def test_render_text_guide_section(tmp_path: Path, temp_modules: list[str]) -> None:
    guide_cls = _make_guide(tmp_path, temp_modules, class_name="RiskAssessmentFramework", content="Use four dimensions.\n")

    class GuideSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = RenderRole
        task = "Task"

        guides = (guide_cls,)

    rendered = render_text(GuideSpec())
    assert "# Reference: Risk Assessment Framework" in rendered
    assert "Use four dimensions." in rendered


def test_render_text_guide_title_dedup(tmp_path: Path, temp_modules: list[str]) -> None:
    """Guide content starting with same title as derived PascalCase title is deduplicated."""
    guide_cls = _make_guide(
        tmp_path,
        temp_modules,
        class_name="RiskAssessmentFramework",
        content="Risk Assessment Framework\nUse four dimensions.\n",
    )

    class GuideSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = RenderRole
        task = "Task"

        guides = (guide_cls,)

    rendered = render_text(GuideSpec())
    assert "# Reference: Risk Assessment Framework" in rendered
    # The title "Risk Assessment Framework" should not appear twice
    assert "Use four dimensions." in rendered
    # Check that the first content line was stripped (no duplicate after header)
    ref_section = rendered.split("# Reference: Risk Assessment Framework")[1]
    assert ref_section.strip().startswith("Use four dimensions.")


def test_render_text_guide_keeps_non_matching_title(tmp_path: Path, temp_modules: list[str]) -> None:
    guide_cls = _make_guide(tmp_path, temp_modules, class_name="MethodPlaybook", content="Different Heading\nBody line\n")

    class GuideSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = RenderRole
        task = "Task"

        guides = (guide_cls,)

    rendered = render_text(GuideSpec())
    assert "# Reference: Method Playbook" in rendered
    assert "Different Heading" in rendered


def test_render_text_output_structure_result_in_output_rules() -> None:
    """<result> instruction appears in Output Rules, not Output Structure."""

    class StructSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = RenderRole
        task = "Task"

        output_structure = "## Summary\n## Details"

    rendered = render_text(StructSpec())
    # Result tag instruction is in Output Rules
    assert "# Output Rules" in rendered
    assert "Write your complete response inside <result> tags" in rendered
    # Output Structure has just the structure content
    assert "# Output Structure\n\n## Summary\n## Details" in rendered
    idx_structure = rendered.index("# Output Structure")
    structure_section = rendered[idx_structure:]
    assert "<result>your response content</result>" not in structure_section


def test_render_text_output_structure_section() -> None:
    class StructSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = RenderRole
        task = "Task"

        output_structure = "## Summary\n## Details"

    rendered = render_text(StructSpec())
    assert "# Output Structure\n\n## Summary\n## Details" in rendered


def test_render_text_output_rules_section() -> None:
    class OutputRulesSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = RenderRole
        task = "Task"

        output_rules = (RenderOutputRule,)

    rendered = render_text(OutputRulesSpec())
    assert "# Output Rules\n\n1. Use bullet list format" in rendered
    assert "# Output Structure" not in rendered


def test_render_text_no_output_sections_when_empty() -> None:
    class NoOutputSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = RenderRole
        task = "Task"

    rendered = render_text(NoOutputSpec())
    assert "# Output Rules" not in rendered
    assert "# Output Structure" not in rendered


def test_render_text_output_rules_before_structure() -> None:
    class FullOutputSpec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = RenderRole
        task = "Task"

        output_structure = "## Section"
        output_rules = (RenderOutputRule,)

    rendered = render_text(FullOutputSpec())
    rules_pos = rendered.index("# Output Rules")
    structure_pos = rendered.index("# Output Structure")
    assert rules_pos < structure_pos
    assert "1. Use bullet list format" in rendered
    assert "## Section" in rendered
    assert _RESULT_OPEN in rendered


def test_render_text_documents_empty_list_uses_preview() -> None:
    """When documents=[] (truthy empty list), it takes the 'documents is not None' path."""

    class DocSpec(PromptSpec):
        """Doc."""

        input_documents = (RenderDoc,)
        role = RenderRole
        task = "Task"

    rendered = render_text(DocSpec(), documents=[])
    # documents=[] is not None but is falsy for content
    assert "Documents provided in context:" in rendered


# ---------------------------------------------------------------------------
# render_text: follow-up specs
# ---------------------------------------------------------------------------


def test_render_follow_up_skips_role() -> None:
    """Follow-up spec with role=None renders no Role section."""

    class InitSpec(PromptSpec):
        """Init."""

        input_documents = ()
        role = RenderRole
        task = "init"

    class FollowSpec(PromptSpec, follows=InitSpec):
        """Follow."""

        task = "continue"

    rendered = render_text(FollowSpec())
    assert "# Role" not in rendered
    assert "# Task" in rendered


def test_render_follow_up_with_role() -> None:
    """Follow-up spec with explicit role renders Role section."""

    class InitSpec(PromptSpec):
        """Init."""

        input_documents = ()
        role = RenderRole
        task = "init"

    class FollowSpec(PromptSpec, follows=InitSpec):
        """Follow."""

        role = RenderRole
        task = "continue"

    rendered = render_text(FollowSpec())
    assert "# Role" in rendered


def test_render_text_no_follows_note() -> None:
    """render_text does NOT include follows note (only render_preview does)."""

    class InitSpec(PromptSpec):
        """Init."""

        input_documents = ()
        role = RenderRole
        task = "init"

    class FollowSpec(PromptSpec, follows=InitSpec):
        """Follow."""

        task = "continue"

    rendered = render_text(FollowSpec())
    assert "[Follows:" not in rendered


# ---------------------------------------------------------------------------
# render_preview
# ---------------------------------------------------------------------------


def test_render_preview_uses_placeholders() -> None:
    class PlaceholderSpec(PromptSpec):
        """Doc."""

        input_documents = (RenderDoc,)
        role = RenderRole
        task = "Task"

        item_id: int = Field(description="Item ID")

    preview = render_preview(PlaceholderSpec)
    assert "**Item ID:**\n{item_id}" in preview
    assert "RenderDoc" in preview


def test_render_preview_include_input_documents_false() -> None:
    class PlaceholderSpec(PromptSpec):
        """Doc."""

        input_documents = (RenderDoc,)
        role = RenderRole
        task = "Task"

        item: str = Field(description="Item")

    preview = render_preview(PlaceholderSpec, include_input_documents=False)
    assert "Documents provided in context:" not in preview
    assert "{item}" in preview


def test_render_preview_shows_follows() -> None:
    class InitSpec(PromptSpec):
        """Init."""

        input_documents = ()
        role = RenderRole
        task = "init"

    class FollowSpec(PromptSpec, follows=InitSpec):
        """Follow."""

        task = "continue"

    preview = render_preview(FollowSpec)
    assert preview.startswith("[Follows: InitSpec]")


# ---------------------------------------------------------------------------
# Full workflow example marked as a documentation example.
# ---------------------------------------------------------------------------


@pytest.mark.ai_docs
def test_render_full_prompt_spec_workflow() -> None:
    """Define components and a PromptSpec, then render it to prompt text."""
    from ai_pipeline_core.prompt_compiler import OutputRule, PromptSpec, Role, Rule, render_text

    class Analyst(Role):
        """Research analyst role."""

        text = "experienced research analyst"

    class CiteEvidence(Rule):
        """Citation requirement."""

        text = "Cite specific evidence from source documents using document IDs"

    class UseProse(OutputRule):
        """Formatting constraint."""

        text = "Use prose paragraphs, not bullet lists"

    class AnalysisSpec(PromptSpec):
        """Analyze source documents for key findings."""

        input_documents = ()
        role = Analyst
        task = "Identify the key findings and assess their significance."
        rules = (CiteEvidence,)
        output_rules = (UseProse,)

        output_structure = "## Key Findings\n## Significance Assessment"
        topic: str = Field(description="Research topic")

    rendered = render_text(AnalysisSpec(topic="Market dynamics"))

    assert "# Role\n\nYou are an experienced research analyst." in rendered
    assert "**Research topic:**\nMarket dynamics" in rendered
    assert "# Rules\n\n1. Cite specific evidence" in rendered
    assert "# Output Rules\n\n1. Use prose paragraphs" in rendered
    assert "# Output Structure\n\n## Key Findings" in rendered


# ---------------------------------------------------------------------------
# Long field value XML wrapping tests
# ---------------------------------------------------------------------------


class TestFieldValueValidation:
    """Regular field values exceeding _MAX_FIELD_VALUE_LENGTH or containing newlines are auto-promoted to multi-line treatment."""

    def test_short_single_line_renders_inline(self):
        """Short single-line values render as plain '**label:**\\nvalue'."""

        class ShortSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = RenderRole
            task = "Task"
            topic: str = Field(description="Research topic")

        rendered = render_text(ShortSpec(topic="Market dynamics"))
        assert "**Research topic:**\nMarket dynamics" in rendered
        assert "<topic>" not in rendered

    def test_long_value_treated_as_multi_line(self):
        """Values exceeding _MAX_FIELD_VALUE_LENGTH are auto-promoted to multi-line (no ValueError)."""

        class LongSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = RenderRole
            task = "Task"
            review_text: str = Field(description="Review feedback")

        long_value = "x" * (_MAX_FIELD_VALUE_LENGTH + 1)
        rendered = render_text(LongSpec(review_text=long_value))
        # Value is NOT inlined in Context
        assert long_value not in rendered
        # Reference placeholder is present
        assert "(provided in <review_text> tags in previous message)" in rendered

    def test_multiline_value_treated_as_multi_line(self):
        """Multiline values are auto-promoted to multi-line (no ValueError)."""

        class MultilineSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = RenderRole
            task = "Task"
            feedback: str = Field(description="Reviewer feedback")

        rendered = render_text(MultilineSpec(feedback="First line\nSecond line"))
        # Value is NOT inlined
        assert "First line\nSecond line" not in rendered
        # Reference placeholder is present
        assert "(provided in <feedback> tags in previous message)" in rendered

    def test_exactly_at_limit_not_rejected(self):
        """Value exactly at _MAX_FIELD_VALUE_LENGTH is accepted."""

        class ExactSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = RenderRole
            task = "Task"
            text: str = Field(description="Some text")

        exact_value = "x" * _MAX_FIELD_VALUE_LENGTH
        rendered = render_text(ExactSpec(text=exact_value))
        assert f"**Some text:**\n{exact_value}" in rendered
        assert "<text>" not in rendered

    def test_long_value_logs_warning(self, caplog: pytest.LogCaptureFixture):
        """Long field values emit a warning when auto-promoted."""
        import logging

        class WarnSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = RenderRole
            task = "Task"
            review: str = Field(description="Review")

        long_value = "x" * (_MAX_FIELD_VALUE_LENGTH + 1)
        with caplog.at_level(logging.WARNING):
            render_text(WarnSpec(review=long_value))

        assert len(caplog.records) == 1
        msg = caplog.records[0].message
        assert "review" in msg
        assert "WarnSpec" in msg
        assert "MultiLineField" in msg

    def test_multiline_value_logs_warning(self, caplog: pytest.LogCaptureFixture):
        """Multiline field values emit a warning when auto-promoted."""
        import logging

        class WarnMultiSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = RenderRole
            task = "Task"
            note: str = Field(description="Note")

        with caplog.at_level(logging.WARNING):
            render_text(WarnMultiSpec(note="line1\nline2"))

        assert len(caplog.records) == 1
        msg = caplog.records[0].message
        assert "note" in msg
        assert "WarnMultiSpec" in msg

    def test_short_value_no_warning(self, caplog: pytest.LogCaptureFixture):
        """Short single-line values produce no warning."""
        import logging

        class NoWarnSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = RenderRole
            task = "Task"
            item: str = Field(description="Item")

        with caplog.at_level(logging.WARNING):
            render_text(NoWarnSpec(item="short"))

        assert len(caplog.records) == 0

    def test_auto_promoted_field_in_multi_line_messages(self):
        """Auto-promoted regular fields appear in _render_multi_line_messages output."""
        from ai_pipeline_core.prompt_compiler.render import _render_multi_line_messages

        class AutoSpec(PromptSpec):
            """Doc."""

            input_documents = ()
            role = RenderRole
            task = "Task"
            feedback: str = Field(description="Feedback")

        spec = AutoSpec(feedback="line1\nline2")
        messages = _render_multi_line_messages(spec)
        assert len(messages) == 1
        assert messages[0] == ("feedback", "<feedback>line1\nline2</feedback>")
