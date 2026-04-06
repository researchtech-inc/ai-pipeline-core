"""Tests for prompt_compiler.cli (CLI tool)."""

import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

import ai_pipeline_core.prompt_compiler.cli as prompt_cli
from ai_pipeline_core.documents import Document
from ai_pipeline_core.prompt_compiler.cli import (
    _SKIP_DIRS,
    _ensure_importable,
    _file_defines_class,
    _file_may_contain_specs,
    _iter_python_files,
    _module_name_from_path,
    _output_label,
    _print_table,
    _resolve_spec_class,
    main,
)
from ai_pipeline_core.prompt_compiler.components import OutputRule, Role, Rule
from ai_pipeline_core.prompt_compiler.spec import PromptSpec


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class CliRole(Role):
    """CLI test role."""

    text = "experienced analyst"


class CliRule(Rule):
    """CLI test rule."""

    text = "Be precise\nCite evidence"


class CliOutputRule(OutputRule):
    """CLI test output rule."""

    text = "Use bullet lists"


class CliDoc(Document):
    """CLI input document."""


class CliPayload(BaseModel):
    """Structured output."""

    answer: str


# Module-level specs so _resolve_spec_class can find them via getattr.


class MinimalInspectSpec(PromptSpec):
    """A minimal spec for inspect testing."""

    input_documents = ()
    role = CliRole
    task = "Do the task"


class StructuredInspectSpec(PromptSpec[CliPayload]):
    """Structured output spec."""

    input_documents = ()
    role = CliRole
    task = "Return data"


# ---------------------------------------------------------------------------
# _iter_python_files
# ---------------------------------------------------------------------------


def test_skip_dirs_includes_tmp() -> None:
    """`.tmp` must be skipped to avoid scanning vendored dependency source trees."""
    assert ".tmp" in _SKIP_DIRS


def test_iter_python_files_skips_tmp_dirs(tmp_path: Path) -> None:
    (tmp_path / ".tmp" / "deps").mkdir(parents=True)
    (tmp_path / ".tmp" / "deps" / "mod.py").write_text("pass", encoding="utf-8")
    (tmp_path / "real.py").write_text("pass", encoding="utf-8")
    result = _iter_python_files(tmp_path)
    names = {f.name for f in result}
    assert "real.py" in names
    assert "mod.py" not in names


def test_iter_python_files_finds_py_files(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("pass", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("pass", encoding="utf-8")
    result = _iter_python_files(tmp_path)
    names = {f.name for f in result}
    assert "a.py" in names
    assert "b.py" in names


def test_iter_python_files_skips_excluded_dirs(tmp_path: Path) -> None:
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "cached.py").write_text("pass", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "pkg.py").write_text("pass", encoding="utf-8")
    result = _iter_python_files(tmp_path)
    assert result == []


# ---------------------------------------------------------------------------
# _module_name_from_path
# ---------------------------------------------------------------------------


def test_module_name_from_regular_file(tmp_path: Path) -> None:
    assert _module_name_from_path(tmp_path / "pkg" / "mod.py", tmp_path) == "pkg.mod"


def test_module_name_from_init_file(tmp_path: Path) -> None:
    assert _module_name_from_path(tmp_path / "pkg" / "__init__.py", tmp_path) == "pkg"


def test_module_name_from_unrelated_path(tmp_path: Path) -> None:
    assert _module_name_from_path(Path("/unrelated/mod.py"), tmp_path) is None


def test_module_name_from_root_init(tmp_path: Path) -> None:
    # __init__.py directly at root → parts becomes empty after stripping __init__
    assert _module_name_from_path(tmp_path / "__init__.py", tmp_path) is None


# ---------------------------------------------------------------------------
# _file_defines_class
# ---------------------------------------------------------------------------


def test_file_defines_class_found(tmp_path: Path) -> None:
    f = tmp_path / "mod.py"
    f.write_text("class Foo:\n    pass\n", encoding="utf-8")
    assert _file_defines_class(f, "Foo") is True


def test_file_defines_class_not_found(tmp_path: Path) -> None:
    f = tmp_path / "mod.py"
    f.write_text("class Bar:\n    pass\n", encoding="utf-8")
    assert _file_defines_class(f, "Foo") is False


def test_file_defines_class_syntax_error(tmp_path: Path) -> None:
    f = tmp_path / "bad.py"
    f.write_text("def broken(:\n", encoding="utf-8")
    assert _file_defines_class(f, "Foo") is False


def test_file_defines_class_missing_file(tmp_path: Path) -> None:
    assert _file_defines_class(tmp_path / "missing.py", "Foo") is False


def test_file_defines_class_ignores_nested(tmp_path: Path) -> None:
    f = tmp_path / "mod.py"
    f.write_text("def outer():\n    class Inner:\n        pass\n", encoding="utf-8")
    # AST only checks top-level (tree.body)
    assert _file_defines_class(f, "Inner") is False


# ---------------------------------------------------------------------------
# _file_may_contain_specs
# ---------------------------------------------------------------------------


def test_file_may_contain_specs_positive(tmp_path: Path) -> None:
    f = tmp_path / "spec.py"
    f.write_text("class MySpec(PromptSpec):\n    pass\n", encoding="utf-8")
    assert _file_may_contain_specs(f) is True


def test_file_may_contain_specs_negative(tmp_path: Path) -> None:
    f = tmp_path / "other.py"
    f.write_text("x = 1\n", encoding="utf-8")
    assert _file_may_contain_specs(f) is False


def test_file_may_contain_specs_missing_file(tmp_path: Path) -> None:
    assert _file_may_contain_specs(tmp_path / "missing.py") is False


# ---------------------------------------------------------------------------
# _output_label
# ---------------------------------------------------------------------------


def test_output_label_str_plain() -> None:
    class Spec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = CliRole
        task = "task"

    assert _output_label(Spec) == "str"


def test_output_label_str_structure() -> None:
    class Spec(PromptSpec):
        """Doc."""

        input_documents = ()
        role = CliRole
        task = "task"

        output_structure = "## Section"

    assert _output_label(Spec) == "str [structure]"


def test_output_label_basemodel() -> None:
    class Spec(PromptSpec[CliPayload]):
        """Doc."""

        input_documents = ()
        role = CliRole
        task = "task"

    assert _output_label(Spec) == "CliPayload"


# ---------------------------------------------------------------------------
# _ensure_importable
# ---------------------------------------------------------------------------


def test_ensure_importable_adds_to_sys_path(tmp_path: Path) -> None:
    root_str = str(tmp_path / "unique_root")
    assert root_str not in sys.path
    _ensure_importable(tmp_path / "unique_root")
    assert root_str in sys.path
    sys.path.remove(root_str)


def test_ensure_importable_idempotent(tmp_path: Path) -> None:
    root_str = str(tmp_path)
    sys.path.insert(0, root_str)
    count_before = sys.path.count(root_str)
    _ensure_importable(tmp_path)
    assert sys.path.count(root_str) == count_before
    sys.path.remove(root_str)


# ---------------------------------------------------------------------------
# _print_table
# ---------------------------------------------------------------------------


def test_print_table_empty_rows(capsys: pytest.CaptureFixture[str]) -> None:
    _print_table(["A", "B"], [])
    assert capsys.readouterr().out == ""


def test_print_table_alignment(capsys: pytest.CaptureFixture[str]) -> None:
    _print_table(["Name", "Val"], [["short", "1"], ["longer_name", "22"]])
    out = capsys.readouterr().out
    lines = out.strip().splitlines()
    assert len(lines) == 4  # header, separator, 2 rows
    assert "Name" in lines[0]
    assert "---" in lines[1]
    assert "short" in lines[2]
    assert "longer_name" in lines[3]


# ---------------------------------------------------------------------------
# _resolve_spec_class
# ---------------------------------------------------------------------------


def test_resolve_spec_class_explicit_ref() -> None:
    # Use a spec from the test_api module that's already imported
    cls = _resolve_spec_class("tests.prompt_compiler.test_api:PlainSpec", Path.cwd())
    assert cls.__name__ == "PlainSpec"


def test_resolve_spec_class_explicit_ref_not_a_spec() -> None:
    with pytest.raises(ValueError, match="is not a PromptSpec subclass"):
        _resolve_spec_class("tests.prompt_compiler.test_api:ApiRole", Path.cwd())


def test_resolve_spec_class_not_found() -> None:
    with pytest.raises(ValueError, match="not found"):
        _resolve_spec_class("NonExistentSpecClass99999", Path.cwd())


# ---------------------------------------------------------------------------
# main() — subcommand routing
# ---------------------------------------------------------------------------


def test_main_no_command_returns_1(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 1
    out = capsys.readouterr().out
    assert "Prompt compiler CLI" in out


# ---------------------------------------------------------------------------
# main() — render command
# ---------------------------------------------------------------------------


@pytest.mark.ai_docs
def test_main_render(capsys: pytest.CaptureFixture[str]) -> None:
    ret = main(["render", "tests.prompt_compiler.test_api:PlainSpec"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "# Role" in out
    assert "# Task" in out


def test_main_render_no_input_documents(capsys: pytest.CaptureFixture[str]) -> None:
    ret = main(["render", "--no-input-documents", "tests.prompt_compiler.test_api:PlainSpec"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "# Role" in out


def test_main_render_not_found(capsys: pytest.CaptureFixture[str]) -> None:
    ret = main(["render", "NoSuchSpec12345"])
    assert ret == 1
    err = capsys.readouterr().err
    assert "not found" in err


# ---------------------------------------------------------------------------
# main() — compile command (includes discovery listing)
# ---------------------------------------------------------------------------


@pytest.mark.ai_docs
def test_main_compile_finds_specs(capsys: pytest.CaptureFixture[str]) -> None:
    ret = main(["compile", "--root", str(Path.cwd())])
    assert ret == 0
    out = capsys.readouterr().out
    assert "spec(s) found:" in out
    assert "Name" in out  # table header


def test_main_compile_empty_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ret = main(["compile", "--root", str(tmp_path)])
    assert ret == 0
    capsys.readouterr()  # Consume output; we only verify it doesn't crash


# ---------------------------------------------------------------------------
# main() — inspect command
# ---------------------------------------------------------------------------


@pytest.mark.ai_docs
def test_main_inspect_minimal_spec(capsys: pytest.CaptureFixture[str]) -> None:
    """Inspect a spec with no rules, no guides, no output rules, no fields."""
    ret = main(["inspect", f"{MinimalInspectSpec.__module__}:MinimalInspectSpec"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "MinimalInspectSpec" in out
    assert "A minimal spec for inspect testing." in out
    assert "Module:" in out
    assert "Role: CliRole" in out
    assert '"experienced analyst"' in out
    assert "Input Documents (0):" in out
    assert "(none)" in out
    assert "Dynamic Fields (0):" in out
    assert "Do the task" in out
    assert "Type: str" in out
    assert "Rendered preview:" in out
    assert "tokens" in out
    # No rules/guides/output_rules sections
    assert "Rules (" not in out
    assert "Guides (" not in out
    assert "Output Rules (" not in out
    # Phase and XML Wrapped removed
    assert "Phase:" not in out
    assert "XML Wrapped" not in out


def test_main_inspect_full_spec(capsys: pytest.CaptureFixture[str]) -> None:
    """Inspect a spec with all features: docs, fields, rules, guides, output_structure."""
    # Use FinalVerdictSpec from showcase — it has rules, output_rules, output_structure
    ret = main(["inspect", "examples.showcase_prompt_compiler:FinalVerdictSpec"])
    assert ret == 0
    out = capsys.readouterr().out

    # Header
    assert "FinalVerdictSpec" in out
    assert "Module:" in out

    # Documents
    assert "Input Documents (3):" in out

    # Fields
    assert "Dynamic Fields (1):" in out
    assert "project_name (str): Project name" in out

    # Task
    assert "produce a final verdict" in out

    # Rules — multiline CiteEvidence shows "..."
    assert "Rules (2):" in out
    assert "CiteEvidence:" in out

    # Output
    assert "Type: str [structure]" in out
    assert "## Verdict" in out
    assert "Output Rules (1):" in out
    assert "DontUseMarkdownTables" in out

    # Size
    assert "Rendered preview:" in out
    assert "tokens" in out


def test_main_inspect_basemodel_output(capsys: pytest.CaptureFixture[str]) -> None:
    ret = main(["inspect", f"{StructuredInspectSpec.__module__}:StructuredInspectSpec"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "Type: CliPayload" in out


def test_main_inspect_with_guides(capsys: pytest.CaptureFixture[str]) -> None:
    """Inspect a spec that has guides — covers the guides section rendering."""
    ret = main(["inspect", "examples.showcase_prompt_compiler:IssueOptimisticSpec"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "Guides (2):" in out
    assert "RiskAssessmentFramework" in out
    assert "chars)" in out


def test_main_inspect_not_found(capsys: pytest.CaptureFixture[str]) -> None:
    ret = main(["inspect", "NoSuchSpec12345"])
    assert ret == 1
    err = capsys.readouterr().err
    assert "not found" in err


# ---------------------------------------------------------------------------
# _resolve_spec_class — auto-discovery
# ---------------------------------------------------------------------------


def test_resolve_spec_class_auto_discover() -> None:
    """Auto-discover a spec by class name (no module: prefix)."""
    # PlainSpec is defined in tests.prompt_compiler.test_api which is already imported,
    # so _all_prompt_spec_subclasses will find it after scanning.
    cls = _resolve_spec_class("PlainSpec", Path.cwd())
    assert cls.__name__ == "PlainSpec"


def test_resolve_spec_class_auto_discover_import_needed() -> None:
    """Auto-discover a class that requires importing its module."""
    # IssueOptimisticSpec lives in examples/showcase_prompt_compiler.py
    cls = _resolve_spec_class("IssueOptimisticSpec", Path.cwd())
    assert cls.__name__ == "IssueOptimisticSpec"


# ---------------------------------------------------------------------------
# _discover_all_specs — import errors
# ---------------------------------------------------------------------------


def test_discover_all_specs_reports_import_errors(tmp_path: Path) -> None:
    """Discovery reports import errors without crashing."""
    # Create a package with a file that references PromptSpec but fails to import
    pkg = tmp_path / "broken_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "spec.py").write_text("from ai_pipeline_core.prompt_compiler import PromptSpec\nraise RuntimeError('boom')\n", encoding="utf-8")

    root_str = str(tmp_path)
    sys.path.insert(0, root_str)
    try:
        from ai_pipeline_core.prompt_compiler.cli import _discover_all_specs

        _specs, errors = _discover_all_specs(tmp_path)
        assert any("boom" in e for e in errors)
    finally:
        sys.path.remove(root_str)
        sys.modules.pop("broken_pkg", None)
        sys.modules.pop("broken_pkg.spec", None)


def test_discover_all_specs_suppresses_syntax_warnings(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """SyntaxWarning from third-party import hooks (e.g. beartype) must not leak to stderr."""
    pkg = tmp_path / "warny_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    # Module that references PromptSpec and contains a non-raw string with invalid escape
    (pkg / "spec.py").write_text(
        'from ai_pipeline_core.prompt_compiler import PromptSpec\nx = r"test\\.pattern"\n',
        encoding="utf-8",
    )

    root_str = str(tmp_path)
    sys.path.insert(0, root_str)
    try:
        from ai_pipeline_core.prompt_compiler.cli import _discover_all_specs

        _discover_all_specs(tmp_path)
        captured = capsys.readouterr()
        assert "SyntaxWarning" not in captured.err
    finally:
        sys.path.remove(root_str)
        sys.modules.pop("warny_pkg", None)
        sys.modules.pop("warny_pkg.spec", None)


def test_discover_all_specs_skips_none_module_names(tmp_path: Path) -> None:
    """Files at root __init__.py get module_name=None and are skipped."""
    init = tmp_path / "__init__.py"
    init.write_text("from ai_pipeline_core.prompt_compiler import PromptSpec\n", encoding="utf-8")

    from ai_pipeline_core.prompt_compiler.cli import _discover_all_specs

    # Should not crash — just skip the file
    _discover_all_specs(tmp_path)


# ---------------------------------------------------------------------------
# main() — compile command
# ---------------------------------------------------------------------------


def test_main_compile_creates_prompts_dir(capsys: pytest.CaptureFixture[str]) -> None:
    """Compile writes rendered prompts to .prompts/ and reports count."""
    ret = main(["compile", "--root", str(Path.cwd())])
    assert ret == 0
    out = capsys.readouterr().out
    assert "Compiled" in out
    assert ".prompts/" in out

    prompts_dir = Path.cwd() / ".prompts"
    assert prompts_dir.is_dir()
    md_files = sorted(prompts_dir.glob("*.md"))
    assert len(md_files) > 0

    # Each file should be named ClassName.md and contain rendered prompt text
    for md_file in md_files:
        content = md_file.read_text(encoding="utf-8")
        assert "# Task" in content


def test_main_compile_file_content_matches_render(capsys: pytest.CaptureFixture[str]) -> None:
    """Compiled file content must match render_preview output for the same spec."""
    main(["compile", "--root", str(Path.cwd())])
    capsys.readouterr()

    from ai_pipeline_core.prompt_compiler.render import render_preview

    # Check a known spec
    prompts_dir = Path.cwd() / ".prompts"
    compiled = (prompts_dir / "MinimalInspectSpec.md").read_text(encoding="utf-8")
    expected = render_preview(MinimalInspectSpec)
    assert compiled == expected


def test_main_compile_removes_stale_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Compile removes stale .prompts/ directory entirely before writing."""
    prompts_dir = tmp_path / ".prompts"
    prompts_dir.mkdir()
    stale_file = prompts_dir / "DeletedSpec.md"
    stale_file.write_text("old content", encoding="utf-8")

    # Compile in tmp_path (no specs found) — stale file should be removed
    ret = main(["compile", "--root", str(tmp_path)])
    assert ret == 0
    assert not stale_file.exists()


def test_main_compile_tolerates_concurrent_output_dir_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compile tolerates another process deleting .prompts/ during cleanup."""
    prompts_dir = tmp_path / ".prompts"
    prompts_dir.mkdir()
    (prompts_dir / "stale.md").write_text("stale", encoding="utf-8")

    real_rmtree = prompt_cli.shutil.rmtree

    def disappearing_rmtree(path: Path, *args: Any, **kwargs: Any) -> None:
        real_rmtree(path, *args, **kwargs)
        raise FileNotFoundError(path)

    monkeypatch.setattr(prompt_cli.shutil, "rmtree", disappearing_rmtree)

    ret = main(["compile", "--root", str(tmp_path)])

    assert ret == 0
    assert prompts_dir.is_dir()
    assert not (prompts_dir / "stale.md").exists()


def test_main_compile_idempotent(capsys: pytest.CaptureFixture[str]) -> None:
    """Running compile twice produces identical output files."""
    import gc

    gc.collect()  # Flush weakly-referenced PromptSpec classes from prior tests

    root = Path.cwd()
    main(["compile", "--root", str(root)])
    capsys.readouterr()

    prompts_dir = root / ".prompts"
    first_run = {f.name: f.read_text(encoding="utf-8") for f in prompts_dir.glob("*.md")}

    main(["compile", "--root", str(root)])
    capsys.readouterr()

    second_run = {f.name: f.read_text(encoding="utf-8") for f in prompts_dir.glob("*.md")}
    assert first_run == second_run
