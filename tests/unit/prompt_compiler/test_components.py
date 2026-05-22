"""Tests for prompt_compiler.components (Role, Rule, OutputRule, Guide)."""

import sys
from collections.abc import Generator
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest

from ai_pipeline_core.prompt_compiler.components import (
    _MAX_RULE_LINES,
    Guide,
    OutputRule,
    Role,
    Rule,
    _require_docstring,
    _require_text,
)


@pytest.fixture
def temp_modules() -> Generator[list[str]]:
    """Track fake modules added to sys.modules and clean up after test."""
    created: list[str] = []
    yield created
    for module_name in created:
        sys.modules.pop(module_name, None)


def _register_module(temp_modules: list[str], module_name: str, module_file: Path | None = None) -> None:
    module = ModuleType(module_name)
    if module_file is not None:
        module.__file__ = str(module_file)
    sys.modules[module_name] = module
    temp_modules.append(module_name)


def _build_guide_class(module_name: str, class_name: str, template: str, doc: str = "Guide doc.") -> type[Guide]:
    return type(class_name, (Guide,), {"__module__": module_name, "__doc__": doc, "template": template})


# ---------------------------------------------------------------------------
# _require_docstring
# ---------------------------------------------------------------------------


def test_require_docstring_rejects_none() -> None:
    cls = type("NoDocClass", (), {"__doc__": None})
    with pytest.raises(TypeError, match="must define a non-empty docstring"):
        _require_docstring(cls, kind="Role")


def test_require_docstring_rejects_whitespace() -> None:
    cls = type("BlankDocClass", (), {"__doc__": "   "})
    with pytest.raises(TypeError, match="must define a non-empty docstring"):
        _require_docstring(cls, kind="Rule")


# ---------------------------------------------------------------------------
# _require_text
# ---------------------------------------------------------------------------


def test_require_text_normalizes_and_sets_class_attribute() -> None:
    cls = type("TextClass", (), {"text": " \n    line one\n    line two\n"})
    _require_text(cls, kind="Rule", max_lines=5)
    assert cls.text == "line one\nline two"


def test_require_text_rejects_non_string() -> None:
    cls = type("BadTextClass", (), {"text": 123})
    with pytest.raises(TypeError, match="must define 'text' as a ClassVar"):
        _require_text(cls, kind="Role")


def test_require_text_rejects_missing_text() -> None:
    cls = type("MissingTextClass", (), {})
    with pytest.raises(TypeError, match="must define 'text' as a ClassVar"):
        _require_text(cls, kind="Role")


def test_require_text_rejects_empty_after_strip() -> None:
    cls = type("EmptyTextClass", (), {"text": " \n \t"})
    with pytest.raises(TypeError, match="has empty 'text'"):
        _require_text(cls, kind="Rule")


def test_require_text_rejects_too_many_lines() -> None:
    text = "\n".join(f"line {i}" for i in range(1, _MAX_RULE_LINES + 2))
    cls = type("LongTextClass", (), {"text": text})
    with pytest.raises(TypeError, match=f"exceeds {_MAX_RULE_LINES} lines"):
        _require_text(cls, kind="Rule", max_lines=_MAX_RULE_LINES)


def test_require_text_no_max_lines_allows_any_length() -> None:
    text = "\n".join(f"line {i}" for i in range(100))
    cls = type("ManyLinesClass", (), {"text": text})
    _require_text(cls, kind="Guide")
    assert cls.text == text


# ---------------------------------------------------------------------------
# Role
# ---------------------------------------------------------------------------


@pytest.mark.ai_docs
def test_role_valid() -> None:
    class ValidRole(Role):
        """A valid role."""

        text = "Expert engineer"

    assert ValidRole.text == "Expert engineer"


def test_role_missing_docstring() -> None:
    with pytest.raises(TypeError, match="must define a non-empty docstring"):

        class NoDocRole(Role):
            text = "valid"


def test_role_empty_docstring() -> None:
    with pytest.raises(TypeError, match="must define a non-empty docstring"):

        class EmptyDocRole(Role):
            """ """

            text = "valid"


def test_role_missing_text() -> None:
    with pytest.raises(TypeError, match="must define 'text' as a ClassVar"):

        class NoTextRole(Role):
            """Doc."""


def test_role_text_not_string() -> None:
    with pytest.raises(TypeError, match="must define 'text' as a ClassVar"):

        class BadTypeRole(Role):
            """Doc."""

            text = 123


def test_role_text_empty() -> None:
    with pytest.raises(TypeError, match="has empty 'text'"):

        class EmptyTextRole(Role):
            """Doc."""

            text = "   "


@pytest.mark.parametrize("punctuation", [".", "!", "?"])
def test_role_rejects_terminal_punctuation(punctuation: str) -> None:
    with pytest.raises(TypeError, match="must not end with punctuation"):
        type(
            f"PunctRole{ord(punctuation)}",
            (Role,),
            {"__doc__": "Role doc.", "text": f"expert reviewer{punctuation}"},
        )


def test_role_normalizes_text() -> None:
    class NormalizedRole(Role):
        """Role doc."""

        text = """
            experienced evaluator
        """

    assert NormalizedRole.text == "experienced evaluator"


# ---------------------------------------------------------------------------
# Rule
# ---------------------------------------------------------------------------


@pytest.mark.ai_docs
def test_rule_valid() -> None:
    class ValidRule(Rule):
        """Doc."""

        text = "Do not fail."

    assert ValidRule.text == "Do not fail."


def test_rule_accepts_exactly_max_lines() -> None:
    text = "\n".join(f"line {i}" for i in range(1, _MAX_RULE_LINES + 1))
    cls = type("ExactLinesRule", (Rule,), {"__doc__": "Doc.", "text": text})
    assert cls.text == text


def test_rule_rejects_too_many_lines() -> None:
    text = "\n".join(f"line {i}" for i in range(1, _MAX_RULE_LINES + 2))
    with pytest.raises(TypeError, match=f"exceeds {_MAX_RULE_LINES} lines"):
        type("LongRule", (Rule,), {"__doc__": "Doc.", "text": text})


def test_rule_missing_docstring() -> None:
    with pytest.raises(TypeError, match="must define a non-empty docstring"):

        class NoDoc(Rule):
            text = "valid"


# ---------------------------------------------------------------------------
# OutputRule
# ---------------------------------------------------------------------------


def test_output_rule_valid() -> None:
    class ValidOutputRule(OutputRule):
        """Doc."""

        text = "Output JSON."

    assert ValidOutputRule.text == "Output JSON."


def test_output_rule_rejects_too_many_lines() -> None:
    text = "\n".join(f"line {i}" for i in range(1, _MAX_RULE_LINES + 2))
    with pytest.raises(TypeError, match=f"exceeds {_MAX_RULE_LINES} lines"):
        type("LongOutput", (OutputRule,), {"__doc__": "Doc.", "text": text})


def test_output_rule_missing_docstring() -> None:
    with pytest.raises(TypeError, match="must define a non-empty docstring"):

        class NoDocOutputRule(OutputRule):
            text = "valid"


# ---------------------------------------------------------------------------
# Guide
# ---------------------------------------------------------------------------


def test_guide_valid(tmp_path: Path, temp_modules: list[str]) -> None:
    template_file = tmp_path / "guide.txt"
    template_file.write_text("## Guide Content\nSome info.", encoding="utf-8")

    module_name = f"guide_mod_{uuid4().hex}"
    _register_module(temp_modules, module_name, module_file=tmp_path / "module.py")

    guide_cls = _build_guide_class(module_name, "ValidGuide", "guide.txt")
    assert "## Guide Content" in guide_cls.render()
    assert guide_cls._resolved_path == template_file.resolve()


def test_guide_missing_docstring() -> None:
    with pytest.raises(TypeError, match="must define a non-empty docstring"):

        class NoDocGuide(Guide):
            template = "guide.txt"


@pytest.mark.parametrize("template_value", [None, "", "   ", 123])
def test_guide_requires_non_empty_string_template(template_value: object) -> None:
    with pytest.raises(TypeError, match="must define 'template' as a ClassVar"):
        type("BadTemplateGuide", (Guide,), {"__module__": __name__, "__doc__": "Guide doc.", "template": template_value})


def test_guide_rejects_absolute_path(tmp_path: Path) -> None:
    absolute = str((tmp_path / "guide.txt").resolve())
    with pytest.raises(TypeError, match="template must be a relative path"):
        type("AbsGuide", (Guide,), {"__module__": __name__, "__doc__": "Guide doc.", "template": absolute})


def test_guide_rejects_when_module_cannot_be_resolved() -> None:
    missing_module = f"missing_mod_{uuid4().hex}"
    with pytest.raises(TypeError, match="cannot resolve module file"):
        _build_guide_class(missing_module, "NoModuleGuide", "guide.txt")


def test_guide_rejects_when_module_has_no_file(temp_modules: list[str]) -> None:
    module_name = f"mod_without_file_{uuid4().hex}"
    _register_module(temp_modules, module_name, module_file=None)
    with pytest.raises(TypeError, match="cannot resolve module file"):
        _build_guide_class(module_name, "NoFileGuide", "guide.txt")


def test_guide_rejects_missing_template_file(tmp_path: Path, temp_modules: list[str]) -> None:
    module_name = f"mod_missing_{uuid4().hex}"
    _register_module(temp_modules, module_name, module_file=tmp_path / "module.py")
    with pytest.raises(TypeError, match="template not found"):
        _build_guide_class(module_name, "MissingGuide", "missing.txt")


def test_guide_rejects_h1_headers_with_line_number(tmp_path: Path, temp_modules: list[str]) -> None:
    template_file = tmp_path / "bad_header.txt"
    template_file.write_text("Intro\n# Bad H1\n## Good H2\n", encoding="utf-8")

    module_name = f"mod_h1_{uuid4().hex}"
    _register_module(temp_modules, module_name, module_file=tmp_path / "module.py")

    with pytest.raises(TypeError, match=r"line 2 uses '# ' header"):
        _build_guide_class(module_name, "H1Guide", "bad_header.txt")


def test_guide_allows_h2_and_deeper_headers(tmp_path: Path, temp_modules: list[str]) -> None:
    template_file = tmp_path / "ok_header.txt"
    template_file.write_text("## H2 header\n### H3 header\n#### H4 header\n", encoding="utf-8")

    module_name = f"mod_h2_{uuid4().hex}"
    _register_module(temp_modules, module_name, module_file=tmp_path / "module.py")

    guide_cls = _build_guide_class(module_name, "OkHeaderGuide", "ok_header.txt")
    assert "## H2 header" in guide_cls.render()


def test_guide_content_cached_at_import_time(tmp_path: Path, temp_modules: list[str]) -> None:
    guide_file = tmp_path / "cached_guide.txt"
    original = "## Original content\nBody.\n"
    guide_file.write_text(original, encoding="utf-8")

    module_name = f"mod_cached_{uuid4().hex}"
    _register_module(temp_modules, module_name, module_file=tmp_path / "module.py")

    guide_cls = _build_guide_class(module_name, "CachedGuide", "cached_guide.txt")
    assert guide_cls.render() == original

    # Modify file after import — render() should still return original
    guide_file.write_text("## Changed content\nNew body.\n", encoding="utf-8")
    assert guide_cls.render() == original
