"""Tests for body-file discovery and validation."""

from pathlib import Path

import pytest

from ai_pipeline_core.prompt_contract._body_file import BodyFile, load_body_file


class _FakeClass:
    """Stand-in class used to drive load_body_file resolution."""


def _stub_module(monkeypatch: pytest.MonkeyPatch, *, name: str, file: Path) -> type:
    """Build a class whose ``__module__`` resolves to a stub module with ``__file__=file``."""
    import sys
    import types

    module_name = f"_test_body_file_stub_{name}"
    module = types.ModuleType(module_name)
    module.__file__ = str(file)
    monkeypatch.setitem(sys.modules, module_name, module)

    cls = type(name, (_FakeClass,), {})
    cls.__module__ = module_name
    return cls


def test_exempt_short_circuits_with_empty_body(tmp_path: Path) -> None:
    cls = type("AnyContract", (), {})
    cls.__module__ = "tests.something"
    body = load_body_file(cls, suffix="Contract", kind="PromptContract", exempt=True)
    assert isinstance(body, BodyFile)
    assert body.source == ""
    assert body.format == "none"


def test_missing_file_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    py_file = tmp_path / "definer.py"
    py_file.write_text("# empty")
    cls = _stub_module(monkeypatch, name="MyAnalysisContract", file=py_file)
    with pytest.raises(TypeError, match="expects a body file"):
        load_body_file(cls, suffix="Contract", kind="PromptContract", exempt=False)


def test_empty_file_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    py_file = tmp_path / "definer.py"
    py_file.write_text("# empty")
    md_file = tmp_path / "my_analysis.md"
    md_file.write_text("   \n   ")
    cls = _stub_module(monkeypatch, name="MyAnalysisContract", file=py_file)
    with pytest.raises(TypeError, match="body file is empty"):
        load_body_file(cls, suffix="Contract", kind="PromptContract", exempt=False)


def test_h1_header_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    py_file = tmp_path / "definer.py"
    py_file.write_text("# empty")
    md_file = tmp_path / "my_analysis.md"
    md_file.write_text("## Allowed\n\n# Not allowed\n")
    cls = _stub_module(monkeypatch, name="MyAnalysisContract", file=py_file)
    with pytest.raises(TypeError, match="uses '# ' \\(H1\\) at line 3"):
        load_body_file(cls, suffix="Contract", kind="PromptContract", exempt=False)


def test_loads_and_returns_content_verbatim(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Body content is returned verbatim (no whitespace trimming) so author-supplied formatting survives."""
    py_file = tmp_path / "definer.py"
    py_file.write_text("# empty")
    md_file = tmp_path / "my_analysis.md"
    md_file.write_text("## Section\n\nbody text\n")
    cls = _stub_module(monkeypatch, name="MyAnalysisContract", file=py_file)
    body = load_body_file(cls, suffix="Contract", kind="PromptContract", exempt=False)
    assert body.source == "## Section\n\nbody text\n"
    assert body.format == "static"


def test_h1_inside_code_fence_is_allowed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The H1 ban must not fire on lines inside fenced code blocks (e.g. ``# comment`` in a python example)."""
    py_file = tmp_path / "definer.py"
    py_file.write_text("# empty")
    md_file = tmp_path / "my_analysis.md"
    md_file.write_text("## Section\n\nExample python:\n\n```python\n# this is a python comment, not an H1 header\nvalue = 1\n```\n\nMore prose.\n")
    cls = _stub_module(monkeypatch, name="MyAnalysisContract", file=py_file)
    body = load_body_file(cls, suffix="Contract", kind="PromptContract", exempt=False)
    assert "# this is a python comment" in body.source


def test_h1_outside_fenced_block_after_code_still_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A real H1 after a closed fence still fails — fence tracking does not bleed over."""
    py_file = tmp_path / "definer.py"
    py_file.write_text("# empty")
    md_file = tmp_path / "my_analysis.md"
    md_file.write_text("## Section\n\n```python\n# inside fence ok\n```\n\n# Bad H1 after fence\n")
    cls = _stub_module(monkeypatch, name="MyAnalysisContract", file=py_file)
    with pytest.raises(TypeError, match="uses '# ' \\(H1\\)"):
        load_body_file(cls, suffix="Contract", kind="PromptContract", exempt=False)


def test_suffix_strip_pascal_to_snake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``AssessReviewRiskContract`` -> ``assess_review_risk.md`` (Contract suffix stripped)."""
    py_file = tmp_path / "module.py"
    py_file.write_text("# empty")
    md_file = tmp_path / "assess_review_risk.md"
    md_file.write_text("## Body\n\nok\n")
    cls = _stub_module(monkeypatch, name="AssessReviewRiskContract", file=py_file)
    body = load_body_file(cls, suffix="Contract", kind="PromptContract", exempt=False)
    assert body.source.strip() == "## Body\n\nok"
    assert body.format == "static"


def test_methodology_suffix_strip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``ReviewQualityMethodology`` -> ``review_quality.md`` (Methodology suffix stripped)."""
    py_file = tmp_path / "module.py"
    py_file.write_text("# empty")
    md_file = tmp_path / "review_quality.md"
    md_file.write_text("## Body\n\nok\n")
    cls = _stub_module(monkeypatch, name="ReviewQualityMethodology", file=py_file)
    body = load_body_file(cls, suffix="Methodology", kind="Methodology", exempt=False)
    assert body.source.strip() == "## Body\n\nok"
    assert body.format == "static"
