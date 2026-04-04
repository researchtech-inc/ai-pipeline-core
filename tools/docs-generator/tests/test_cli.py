from pathlib import Path

from docs_generator.cli import (
    README_FILENAME,
    TEST_DIR_OVERRIDES,
    _discover_modules,
    _render_readme,
    _run_check,
    _run_generate,
    main,
)
from docs_generator.extractor import SymbolTable
from docs_generator.guide_builder import README_ERROR_SIZE, GuideData


def _make_repo(tmp_path):
    src = tmp_path / "ai_pipeline_core"
    src.mkdir()
    tests = tmp_path / "tests"
    tests.mkdir()
    output = tmp_path / ".ai-docs"
    (src / "__init__.py").write_text('"""Module."""\n')
    (src / "mod.py").write_text('"""A module."""\ndef foo():\n    """A func."""\n    pass\n')
    (tests / "test_mod.py").write_text("def test_foo(): pass\n")
    return src, tests, output


def _empty_guide_data(module_name):
    return GuideData(
        module_name=module_name,
        classes=[],
        functions=[],
        external_bases=set(),
        normal_examples=[],
        error_examples=[],
    )


def test_generate_creates_output_dir(tmp_path):
    src, tests, output = _make_repo(tmp_path)
    assert not output.exists()
    _run_generate(src, tests, output, tmp_path)
    assert output.is_dir()


def test_generate_writes_guides(tmp_path, monkeypatch):
    src, tests, output = _make_repo(tmp_path)
    table = SymbolTable()

    from docs_generator.extractor import FunctionInfo

    func = FunctionInfo(
        name="foo",
        signature="()",
        docstring="A func.",
        source="def foo(): ...",
        is_public=True,
        is_async=False,
        line_count=3,
        module_path="test",
    )

    def mock_build_symbol_table(source_dir):
        return table

    def mock_build_guide(module_name, source_dir, tests_dir, tbl, overrides, repo_root=None):
        data = _empty_guide_data(module_name)
        if module_name == "mod":
            data.functions = [func]
        return data

    def mock_render(data, *, version=""):
        return f"# GUIDE: {data.module_name}\n"

    def mock_manage(data, rendered_content, max_size=51200):
        return rendered_content

    monkeypatch.setattr(
        "docs_generator.cli.build_symbol_table",
        mock_build_symbol_table,
    )
    monkeypatch.setattr("docs_generator.cli.build_guide", mock_build_guide)
    monkeypatch.setattr("docs_generator.cli.render_guide", mock_render)
    monkeypatch.setattr("docs_generator.cli.manage_guide_size", mock_manage)

    result = _run_generate(src, tests, output, tmp_path)
    assert result == 0
    guide_files = [f for f in output.glob("*.md") if f.name != README_FILENAME]
    assert len(guide_files) > 0
    assert (output / README_FILENAME).exists()


def test_generate_writes_intro(tmp_path, monkeypatch):
    src, tests, output = _make_repo(tmp_path)
    table = SymbolTable()

    def mock_build_symbol_table(source_dir):
        return table

    def mock_build_guide(module_name, source_dir, tests_dir, tbl, overrides, repo_root=None):
        return _empty_guide_data(module_name)

    monkeypatch.setattr(
        "docs_generator.cli.build_symbol_table",
        mock_build_symbol_table,
    )
    monkeypatch.setattr("docs_generator.cli.build_guide", mock_build_guide)

    _run_generate(src, tests, output, tmp_path)
    assert (output / README_FILENAME).exists()


def test_generate_skips_empty_modules(tmp_path, monkeypatch):
    src, tests, output = _make_repo(tmp_path)
    table = SymbolTable()

    def mock_build_symbol_table(source_dir):
        return table

    def mock_build_guide(module_name, source_dir, tests_dir, tbl, overrides, repo_root=None):
        return _empty_guide_data(module_name)

    monkeypatch.setattr(
        "docs_generator.cli.build_symbol_table",
        mock_build_symbol_table,
    )
    monkeypatch.setattr("docs_generator.cli.build_guide", mock_build_guide)

    _run_generate(src, tests, output, tmp_path)
    # All modules are empty, so no guide .md files (only README.md)
    guide_files = [f for f in output.glob("*.md") if f.name != README_FILENAME]
    assert guide_files == []


def test_generate_cleans_stale_files(tmp_path, monkeypatch):
    src, tests, output = _make_repo(tmp_path)
    output.mkdir()
    (output / "old_module.md").write_text("stale content")

    table = SymbolTable()

    def mock_build_symbol_table(source_dir):
        return table

    def mock_build_guide(module_name, source_dir, tests_dir, tbl, overrides, repo_root=None):
        return _empty_guide_data(module_name)

    monkeypatch.setattr(
        "docs_generator.cli.build_symbol_table",
        mock_build_symbol_table,
    )
    monkeypatch.setattr("docs_generator.cli.build_guide", mock_build_guide)

    _run_generate(src, tests, output, tmp_path)
    assert not (output / "old_module.md").exists()


def test_check_passes_valid(tmp_path):
    src, tests, output = _make_repo(tmp_path)
    output.mkdir()
    (output / "mod.md").write_text("def foo():\n    pass\n")

    result = _run_check(src, output)
    assert result == 0


def test_check_fails_missing_symbols(tmp_path):
    src, tests, output = _make_repo(tmp_path)
    output.mkdir()
    (output / "mod.md").write_text("nothing here\n")

    result = _run_check(src, output)
    assert result == 1


def test_check_fails_missing_dir(tmp_path):
    src, tests, output = _make_repo(tmp_path)
    result = _run_check(src, output)
    assert result == 1


def test_path_auto_detection():
    result = main([
        "--source-dir",
        "/nonexistent/src",
        "--tests-dir",
        "/nonexistent/tests",
        "--output-dir",
        "/tmp/test_ai_docs_out",
        "generate",
    ])
    # Nonexistent dirs produce 0 guides but no crash — returns success
    assert result == 0


def test_path_override(tmp_path):
    src, tests, output = _make_repo(tmp_path)
    result = main([
        "--source-dir",
        str(src),
        "--tests-dir",
        str(tests),
        "--output-dir",
        str(output),
        "generate",
    ])
    assert isinstance(result, int)


def test_module_auto_discovery():
    src_dir = Path(__file__).resolve().parent.parent.parent.parent / "ai_pipeline_core"
    discovered = _discover_modules(src_dir)
    assert "documents" in discovered
    assert "database" in discovered
    assert "llm" in discovered
    assert "observability" in discovered
    assert "pipeline" in discovered
    assert "tracing" not in discovered  # moved into observability/
    assert "docs_generator" not in discovered  # moved to separate package


def test_test_dir_overrides_correctness():
    tests_dir = Path(__file__).resolve().parent.parent.parent.parent / "tests"
    for module_name, override in TEST_DIR_OVERRIDES.items():
        override_dir = tests_dir / override
        assert override_dir.is_dir(), f"Override dir {override_dir} for module {module_name} does not exist"


def test_render_readme_content():
    generated = [("documents", 5000), ("llm", 3000)]
    guide_data_map = {
        "documents": _empty_guide_data("documents"),
        "llm": _empty_guide_data("llm"),
    }
    content = _render_readme(generated, guide_data_map, {}, "")
    assert "<!-- Auto-generated by docs_generator" in content
    assert "# ai-pipeline-core" in content
    assert "documents" in content
    assert "llm" in content


def test_render_readme_with_version():
    generated = [("documents", 5000)]
    guide_data_map = {"documents": _empty_guide_data("documents")}
    content = _render_readme(generated, guide_data_map, {}, "1.2.3")
    assert "v1.2.3" in content


def test_render_readme_module_descriptions():
    generated = [("documents", 5000), ("llm", 3000)]
    descriptions = {"documents": "Document handling.", "llm": "LLM interaction."}
    guide_data_map = {
        "documents": _empty_guide_data("documents"),
        "llm": _empty_guide_data("llm"),
    }
    content = _render_readme(generated, guide_data_map, descriptions, "")
    assert "documents](documents.md) — Document handling." in content
    assert "llm](llm.md) — LLM interaction." in content


def test_render_readme_module_sections():
    from docs_generator.extractor import FunctionInfo

    func = FunctionInfo(
        name="generate",
        signature="(model: str) -> str",
        docstring="Generate LLM output.",
        source="async def generate(model: str) -> str: ...",
        is_public=True,
        is_async=True,
        line_count=1,
        module_path="llm",
    )
    data = _empty_guide_data("llm")
    data.functions = [func]
    generated = [("llm", 3000)]
    guide_data_map = {"llm": data}
    content = _render_readme(generated, guide_data_map, {}, "")
    assert "## [llm](llm.md)" in content
    # Functions rendered as Python code snippets
    assert "async def generate(model: str) -> str:" in content
    assert '"""Generate LLM output."""' in content


def test_render_readme_class_summary():
    from docs_generator.extractor import ClassInfo, MethodInfo

    method = MethodInfo(
        name="send",
        signature="(self, content: str) -> Conversation",
        docstring="Send a message.",
        source="def send(self, content: str) -> Conversation: ...",
        is_property=False,
        is_classmethod=False,
        is_staticmethod=False,
        is_abstract=False,
        is_async=False,
        line_count=1,
    )
    cls = ClassInfo(
        name="Conversation",
        bases=("BaseModel",),
        docstring="Immutable conversation manager.",
        is_public=True,
        class_vars=(),
        methods=(method,),
        validators=(),
        module_path="llm",
    )
    data = _empty_guide_data("llm")
    data.classes = [cls]
    generated = [("llm", 3000)]
    guide_data_map = {"llm": data}
    content = _render_readme(generated, guide_data_map, {}, "")
    # Classes rendered as Python code snippets
    assert "class Conversation(BaseModel):" in content
    assert '"""Immutable conversation manager."""' in content
    # Method stubs include docstring
    assert "def send(self, content: str) -> Conversation:" in content
    assert '"""Send a message."""' in content


def test_render_readme_class_field_descriptions():
    from docs_generator.extractor import ClassInfo

    cls = ClassInfo(
        name="Document",
        bases=("BaseModel",),
        docstring="Document model.",
        is_public=True,
        class_vars=(
            ("name", "str", "", "Filename with extension"),
            ("content", "bytes", "", "Raw binary content"),
            ("derived_from", "tuple[str, ...]", "()", "Content provenance hashes or URLs"),
        ),
        methods=(),
        validators=(),
        module_path="documents",
    )
    data = _empty_guide_data("documents")
    data.classes = [cls]
    generated = [("documents", 3000)]
    guide_data_map = {"documents": data}
    content = _render_readme(generated, guide_data_map, {}, "")
    assert "name: str  # Filename with extension" in content
    assert "content: bytes  # Raw binary content" in content
    assert "derived_from: tuple[str, ...] = ()  # Content provenance hashes or URLs" in content


# ---------------------------------------------------------------------------
# Module purpose helper
# ---------------------------------------------------------------------------


def test_read_module_purpose(tmp_path):
    from docs_generator.cli import _read_module_purpose

    src = tmp_path / "ai_pipeline_core"
    mod = src / "mymod"
    mod.mkdir(parents=True)
    (mod / "__init__.py").write_text('"""Document handling and metadata management.\n\nDetailed description here.\n"""\n')
    purpose = _read_module_purpose(src, "mymod")
    assert purpose == "Document handling and metadata management."


def test_read_module_purpose_missing_init(tmp_path):
    from docs_generator.cli import _read_module_purpose

    src = tmp_path / "ai_pipeline_core"
    src.mkdir(parents=True)
    assert _read_module_purpose(src, "nonexistent") == ""


def test_read_module_purpose_no_docstring(tmp_path):
    from docs_generator.cli import _read_module_purpose

    src = tmp_path / "ai_pipeline_core"
    mod = src / "mymod"
    mod.mkdir(parents=True)
    (mod / "__init__.py").write_text("from .core import MyClass\n")
    assert _read_module_purpose(src, "mymod") == ""


# ---------------------------------------------------------------------------
# Import map helper
# ---------------------------------------------------------------------------


def test_build_import_map(tmp_path):
    from docs_generator.cli import _build_import_map

    src = tmp_path / "ai_pipeline_core"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text(
        "from .documents import Document, Attachment\n"
        "from .llm import Conversation, generate\n"
        "\n"
        '__all__ = ["Document", "Attachment", "Conversation", "generate"]\n'
    )
    result = _build_import_map(src)
    assert "Document" in result.get("documents", [])
    assert "Conversation" in result.get("llm", [])


# ---------------------------------------------------------------------------
# Version reading
# ---------------------------------------------------------------------------


def test_read_version(tmp_path):
    from docs_generator.cli import _read_version

    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\nversion = "1.2.3"\n')
    assert _read_version(tmp_path) == "1.2.3"


def test_read_version_missing_file(tmp_path):
    from docs_generator.cli import _read_version

    assert _read_version(tmp_path) == ""


# ---------------------------------------------------------------------------
# Utility function coverage
# ---------------------------------------------------------------------------


class TestConsolidateCodeBlocks:
    def test_merges_consecutive_blocks(self):
        from docs_generator.cli import _consolidate_code_blocks

        content = "```\n\n```python\ncode\n```"
        result = _consolidate_code_blocks(content)
        assert "```\n\n```python" not in result

    def test_no_change_without_consecutive(self):
        from docs_generator.cli import _consolidate_code_blocks

        content = "```python\ncode\n```\n\nSome text\n```python\nmore code\n```"
        result = _consolidate_code_blocks(content)
        assert result == content


class TestNormalizeWhitespace:
    def test_strips_trailing(self):
        from docs_generator.cli import _normalize_whitespace

        result = _normalize_whitespace("line1   \nline2  \n")
        assert result == "line1\nline2\n\n" or result == "line1\nline2\n"
        assert "   " not in result  # trailing spaces stripped

    def test_ends_with_newline(self):
        from docs_generator.cli import _normalize_whitespace

        result = _normalize_whitespace("content")
        assert result.endswith("\n")


class TestBuildImportMapEdgeCases:
    def test_no_init_file(self, tmp_path):
        from docs_generator.cli import _build_import_map

        result = _build_import_map(tmp_path / "nonexistent")
        assert result == {}

    def test_syntax_error(self, tmp_path):
        from docs_generator.cli import _build_import_map

        src = tmp_path / "pkg"
        src.mkdir()
        (src / "__init__.py").write_text("def broken(:\n")
        result = _build_import_map(src)
        assert result == {}

    def test_no_all(self, tmp_path):
        from docs_generator.cli import _build_import_map

        src = tmp_path / "pkg"
        src.mkdir()
        (src / "__init__.py").write_text("from .mod import Foo\n")
        result = _build_import_map(src)
        assert result == {}

    def test_absolute_import(self, tmp_path):
        from docs_generator.cli import _build_import_map

        src = tmp_path / "ai_pipeline_core"
        src.mkdir()
        (src / "__init__.py").write_text('from ai_pipeline_core.documents import Document\n__all__ = ["Document"]\n')
        result = _build_import_map(src)
        assert "Document" in result.get("documents", [])


class TestBuildModuleImportMap:
    def test_discovers_subpackage_symbols(self, tmp_path):
        from docs_generator.cli import _build_module_import_map

        src = tmp_path / "pkg"
        src.mkdir()
        (src / "__init__.py").write_text('__all__ = ["TopLevel"]\n')
        sub = src / "submod"
        sub.mkdir()
        (sub / "__init__.py").write_text('__all__ = ["SubSymbol"]\n')
        result = _build_module_import_map(src)
        assert "SubSymbol" in result.get("submod", [])

    def test_skips_private_modules(self, tmp_path):
        from docs_generator.cli import _build_module_import_map

        src = tmp_path / "pkg"
        src.mkdir()
        (src / "__init__.py").write_text("__all__ = []\n")
        priv = src / "_private"
        priv.mkdir()
        (priv / "__init__.py").write_text('__all__ = ["Secret"]\n')
        result = _build_module_import_map(src)
        assert "_private" not in result


class TestReadModulePurposeSyntaxError:
    def test_syntax_error_returns_empty(self, tmp_path):
        from docs_generator.cli import _read_module_purpose

        src = tmp_path / "pkg"
        mod = src / "bad"
        mod.mkdir(parents=True)
        (mod / "__init__.py").write_text("def broken(:\n")
        assert _read_module_purpose(src, "bad") == ""


class TestGenerateReadmeSizeLimit:
    def test_generate_fails_when_readme_exceeds_limit(self, tmp_path, monkeypatch):
        src, tests, output = _make_repo(tmp_path)
        table = SymbolTable()

        monkeypatch.setattr("docs_generator.cli.build_symbol_table", lambda _: table)
        monkeypatch.setattr(
            "docs_generator.cli.build_guide",
            lambda *a, **kw: _empty_guide_data(a[0]),
        )

        # Produce a README larger than the limit by injecting a bloated _render_readme
        bloated_content = "x" * (README_ERROR_SIZE + 1024)
        monkeypatch.setattr(
            "docs_generator.cli._render_readme",
            lambda *a, **kw: bloated_content,
        )

        result = _run_generate(src, tests, output, tmp_path)
        assert result == 1

    def test_generate_succeeds_when_readme_under_limit(self, tmp_path, monkeypatch):
        src, tests, output = _make_repo(tmp_path)
        table = SymbolTable()

        monkeypatch.setattr("docs_generator.cli.build_symbol_table", lambda _: table)
        monkeypatch.setattr(
            "docs_generator.cli.build_guide",
            lambda *a, **kw: _empty_guide_data(a[0]),
        )

        small_content = "# Small README\n"
        monkeypatch.setattr(
            "docs_generator.cli._render_readme",
            lambda *a, **kw: small_content,
        )

        result = _run_generate(src, tests, output, tmp_path)
        assert result == 0


class TestMainCheckSubcommand:
    def test_main_no_command(self):
        result = main([])
        assert result == 1

    def test_main_check_missing_output(self, tmp_path):
        src, tests, output = _make_repo(tmp_path)
        result = main([
            "--source-dir",
            str(src),
            "--output-dir",
            str(output),
            "check",
        ])
        assert result == 1


class TestParseAllNames:
    def test_empty_file(self, tmp_path):
        from docs_generator.cli import _parse_init_all

        f = tmp_path / "empty.py"
        f.write_text("")
        result = _parse_init_all(f)
        assert result == set()

    def test_nonexistent_file(self, tmp_path):
        from docs_generator.cli import _parse_init_all

        result = _parse_init_all(tmp_path / "nope.py")
        assert result == set()

    def test_syntax_error(self, tmp_path):
        from docs_generator.cli import _parse_init_all

        f = tmp_path / "bad.py"
        f.write_text("def broken(:\n")
        result = _parse_init_all(f)
        assert result == set()


# ---------------------------------------------------------------------------
# Async method stubs in README
# ---------------------------------------------------------------------------


def test_render_readme_async_method_stub():
    from docs_generator.extractor import ClassInfo, MethodInfo

    method = MethodInfo(
        name="send",
        signature="(self, content: str) -> Conversation",
        docstring="Send a message.",
        source="async def send(self, content: str) -> Conversation: ...",
        is_property=False,
        is_classmethod=False,
        is_staticmethod=False,
        is_abstract=False,
        is_async=True,
        line_count=1,
    )
    cls = ClassInfo(
        name="Conversation",
        bases=("BaseModel",),
        docstring="Immutable conversation manager.",
        is_public=True,
        class_vars=(),
        methods=(method,),
        validators=(),
        module_path="llm",
    )
    data = _empty_guide_data("llm")
    data.classes = [cls]
    generated = [("llm", 3000)]
    guide_data_map = {"llm": data}
    content = _render_readme(generated, guide_data_map, {}, "")
    assert "async def send(self, content: str) -> Conversation:" in content


# ---------------------------------------------------------------------------
# Staticmethod stubs in README
# ---------------------------------------------------------------------------


def test_render_readme_staticmethod_stub():
    from docs_generator.extractor import ClassInfo, MethodInfo

    method = MethodInfo(
        name="build_result",
        signature="(data: dict) -> Result",
        docstring="Build a result.",
        source="@staticmethod\ndef build_result(data: dict) -> Result: ...",
        is_property=False,
        is_classmethod=False,
        is_staticmethod=True,
        is_abstract=False,
        is_async=False,
        line_count=1,
    )
    cls = ClassInfo(
        name="Deployment",
        bases=(),
        docstring="Pipeline deployment.",
        is_public=True,
        class_vars=(),
        methods=(method,),
        validators=(),
        module_path="deployment",
    )
    data = _empty_guide_data("deployment")
    data.classes = [cls]
    generated = [("deployment", 3000)]
    guide_data_map = {"deployment": data}
    content = _render_readme(generated, guide_data_map, {}, "")
    assert "@staticmethod" in content


# ---------------------------------------------------------------------------
# Missing re-exported exceptions
# ---------------------------------------------------------------------------


def test_generate_includes_reexported_symbols(tmp_path):
    """Symbols imported from private modules and re-exported via __all__ must appear in guides."""
    src = tmp_path / "ai_pipeline_core"
    src.mkdir()
    (src / "__init__.py").write_text('"""Module."""\n')

    # Private module defines the class
    priv = src / "_base.py"
    priv.write_text("class BaseError(Exception):\n    pass\n")

    # Public module re-exports it
    exc = src / "exceptions.py"
    exc.write_text(
        '"""Exception hierarchy."""\n'
        "from ai_pipeline_core._base import BaseError\n"
        '__all__ = ["BaseError", "SpecificError"]\n'
        "class SpecificError(BaseError):\n"
        "    pass\n"
    )

    tests = tmp_path / "tests"
    tests.mkdir()
    output = tmp_path / ".ai-docs"

    result = _run_generate(src, tests, output, tmp_path)
    assert result == 0

    guide = (output / "exceptions.md").read_text()
    # Both SpecificError (defined here) and BaseError (re-exported) must appear
    assert "class SpecificError" in guide
    assert "class BaseError" in guide
