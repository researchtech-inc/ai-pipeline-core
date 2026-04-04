"""CLI for AI documentation generation and validation."""

import argparse
import ast
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from docs_generator.extractor import (
    ClassInfo,
    FunctionInfo,
    MethodInfo,
    build_symbol_table,
    format_class_field,
    is_public_name,
    unpack_class_field,
)
from docs_generator.guide_builder import MAX_GUIDE_SIZE, README_ERROR_SIZE, GuideData, build_guide, manage_guide_size, render_guide

__all__ = [
    "PACKAGE_NAME",
    "README_FILENAME",
    "TEST_DIR_OVERRIDES",
    "ValidationResult",
    "main",
    "validate_all",
    "validate_completeness",
    "validate_private_reexports",
    "validate_size",
]

PACKAGE_NAME = "ai_pipeline_core"
README_FILENAME = "README.md"

_EXCLUDED_SYMBOLS: frozenset[str] = frozenset()


_CONSECUTIVE_BLOCKS_RE = re.compile(r"```\n(\s*\n)+```python\n")


def _consolidate_code_blocks(content: str) -> str:
    """Merge consecutive ```python blocks separated only by whitespace."""
    return _CONSECUTIVE_BLOCKS_RE.sub("\n\n", content)


def _normalize_whitespace(content: str) -> str:
    """Strip trailing whitespace from each line and ensure final newline."""
    lines = [line.rstrip() for line in content.splitlines()]
    return "\n".join(lines) + "\n"


TEST_DIR_OVERRIDES: dict[str, str] = {}  # nosemgrep: no-mutable-module-globals


def _discover_modules(source_dir: Path) -> list[str]:
    """Discover all public module groupings from package structure."""
    modules: set[str] = set()
    for py_file in sorted(source_dir.rglob("*.py")):
        if py_file.name.startswith("_") and py_file.name != "__init__.py":
            continue
        relative = py_file.relative_to(source_dir)
        if len(relative.parts) > 1:
            module_name = relative.parts[0]
            if not module_name.startswith("_"):
                modules.add(module_name)
        else:
            modules.add(relative.stem)
    return sorted(modules)


def _read_module_purpose(source_dir: Path, module_name: str) -> str:
    """Read first line of __init__.py docstring for module purpose."""
    init_file = source_dir / module_name / "__init__.py"
    if not init_file.exists():
        return ""
    try:
        tree = ast.parse(init_file.read_text(encoding="utf-8"))
    except SyntaxError:
        return ""
    doc = ast.get_docstring(tree)
    if not doc:
        return ""
    return doc.splitlines()[0].strip()


def _read_version(repo_root: Path) -> str:
    """Read package version from pyproject.toml."""
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.exists():
        return ""
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return data.get("project", {}).get("version", "")


def _build_import_map(source_dir: Path) -> dict[str, list[str]]:  # noqa: C901, PLR0912
    """Parse top-level __init__.py __all__ and map each symbol to its module.

    Returns {module_name: [symbol1, symbol2, ...]} for symbols in __all__.
    """
    init_file = source_dir / "__init__.py"
    if not init_file.exists():
        return {}

    try:
        tree = ast.parse(init_file.read_text(encoding="utf-8"))
    except SyntaxError:
        return {}

    # Collect __all__ names
    all_names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "__all__" and isinstance(node.value, (ast.List, ast.Tuple)):
                all_names = {elt.value for elt in node.value.elts if isinstance(elt, ast.Constant) and isinstance(elt.value, str)}

    if not all_names:
        return {}

    # Map symbol to import source module by scanning imports
    symbol_source: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module and node.names:
            # Extract first-level subpackage from import path
            # Relative: from .documents import ... -> module="documents", level=1
            # Relative nested: from .observability.tracing import ... -> module="observability.tracing", level=1
            # Absolute: from ai_pipeline_core.documents import ... -> module="ai_pipeline_core.documents"
            parts = node.module.split(".")
            if node.level > 0:
                # Relative import: first part is the subpackage
                mod = parts[0]
            elif len(parts) >= 2 and parts[0] == PACKAGE_NAME:
                # Absolute import from our package
                mod = parts[1]
            else:
                mod = parts[0]
            for alias in node.names:
                name = alias.asname if alias.asname else alias.name
                if name in all_names:
                    symbol_source[name] = mod

    # Group by module
    result: dict[str, list[str]] = {}
    for name, mod in sorted(symbol_source.items()):
        result.setdefault(mod, []).append(name)
    return result


def _build_module_import_map(source_dir: Path, excluded_modules: frozenset[str] = frozenset()) -> dict[str, list[str]]:
    """Parse sub-package __init__.py files and map each exported symbol to its module.

    Returns {module_name: [symbol1, symbol2, ...]} for symbols in sub-package __all__
    that are NOT already in the top-level __all__.
    """
    top_level_all = _parse_init_all(source_dir / "__init__.py")
    result: dict[str, list[str]] = {}

    for init_file in sorted(source_dir.glob("*/__init__.py")):
        module_name = init_file.parent.name
        if module_name.startswith("_") or module_name in excluded_modules:
            continue

        sub_all = _parse_init_all(init_file)
        # Only include symbols NOT already at top level
        module_only = sorted(sub_all - top_level_all)
        if module_only:
            result[module_name] = module_only

    return result


def main(argv: list[str] | None = None) -> int:
    """Entry point for AI docs CLI with generate/check subcommands."""
    parser = argparse.ArgumentParser(description="AI documentation generator")
    parser.add_argument("--source-dir", type=Path, help="Source package directory")
    parser.add_argument("--tests-dir", type=Path, help="Tests directory")
    parser.add_argument("--output-dir", type=Path, help="Output .ai-docs directory")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("generate", help="Generate .ai-docs/ documentation")
    subparsers.add_parser("check", help="Validate .ai-docs/ completeness")

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1

    source_dir, tests_dir, output_dir, repo_root = _resolve_paths(args)

    if args.command == "generate":
        return _run_generate(source_dir, tests_dir, output_dir, repo_root)
    return _run_check(source_dir, output_dir)


def _resolve_paths(args: argparse.Namespace) -> tuple[Path, Path, Path, Path]:
    """Resolve source, tests, output directories and repo root from args or auto-detect."""
    cli_file = Path(__file__).resolve()
    # tools/docs-generator/docs_generator/cli.py -> repo root is 4 levels up
    repo_root = cli_file.parent.parent.parent.parent
    source_dir = args.source_dir or (repo_root / "ai_pipeline_core")
    tests_dir = args.tests_dir or (repo_root / "tests")
    output_dir = args.output_dir or (repo_root / ".ai-docs")
    return source_dir, tests_dir, output_dir, repo_root


def _run_generate(source_dir: Path, tests_dir: Path, output_dir: Path, repo_root: Path) -> int:
    """Generate all module guides and README.md."""
    output_dir.mkdir(parents=True, exist_ok=True)

    # Clean stale files
    for existing in output_dir.glob("*.md"):
        existing.unlink()

    version = _read_version(repo_root)
    table = build_symbol_table(source_dir)
    import_map = _build_import_map(source_dir)
    module_import_map = _build_module_import_map(source_dir)
    generated: list[tuple[str, int]] = []
    module_descriptions: dict[str, str] = {}
    guide_data_map: dict[str, GuideData] = {}
    for module_name in _discover_modules(source_dir):
        data = build_guide(module_name, source_dir, tests_dir, table, TEST_DIR_OVERRIDES, repo_root)
        if not data.classes and not data.functions and not data.values:
            print(f"  skip {module_name} (no public symbols)")
            continue

        # Enrich guide data with purpose and imports
        data.purpose = _read_module_purpose(source_dir, module_name)
        guide_symbols = {c.name for c in data.classes} | {f.name for f in data.functions} | {v.name for v in data.values}
        data.imports = sorted(name for name in import_map.get(module_name, []) if name in guide_symbols)
        data.module_imports = sorted(name for name in module_import_map.get(module_name, []) if name in guide_symbols and name not in data.imports)

        if data.purpose:
            module_descriptions[module_name] = data.purpose

        content = render_guide(data, version=version)
        content = manage_guide_size(data, content)
        content = _consolidate_code_blocks(content)
        content = _normalize_whitespace(content)

        guide_path = output_dir / f"{module_name}.md"
        guide_path.write_text(content)
        size = len(content.encode("utf-8"))
        generated.append((module_name, size))
        guide_data_map[module_name] = data
        print(f"  wrote {module_name}.md ({size:,} bytes)")

    # README.md
    readme_content = _render_readme(generated, guide_data_map, module_descriptions, version)
    readme_content = _consolidate_code_blocks(readme_content)
    readme_content = _normalize_whitespace(readme_content)
    generated.append((README_FILENAME, len(readme_content.encode("utf-8"))))
    (output_dir / README_FILENAME).write_text(readme_content)
    print(f"  wrote {README_FILENAME} ({generated[-1][1]:,} bytes)")

    total = sum(size for _, size in generated)
    print(f"\nGenerated {len(generated)} guides ({total:,} bytes total)")

    if generated[-1][1] > README_ERROR_SIZE:
        print(
            f"FAIL: {README_FILENAME} is {generated[-1][1]:,} bytes (max {README_ERROR_SIZE // 1024}KB). "
            f"Reduce public API surface: move helpers to _ prefixed functions, consolidate types.",
            file=sys.stderr,
        )
        return 1
    return 0


def _run_check(source_dir: Path, output_dir: Path) -> int:
    """Validate .ai-docs/ completeness and size."""
    if not output_dir.is_dir():
        print("FAIL: .ai-docs/ directory does not exist. Run 'generate' first.", file=sys.stderr)
        return 1

    result = validate_all(output_dir, source_dir)

    if result.missing_symbols:
        print(f"FAIL: {len(result.missing_symbols)} public symbols missing from guides:")
        for sym in result.missing_symbols:
            print(f"  - {sym}")
    if result.private_reexports:
        print(f"FAIL: {len(result.private_reexports)} symbols in __all__ imported from private modules:")
        for msg in result.private_reexports:
            print(f"  - {msg}")
    if result.size_violations:
        print(f"WARNING: {len(result.size_violations)} guides exceed size limit:")
        for name, size in result.size_violations:
            print(f"  - {name}: {size:,} bytes")

    if result.is_valid:
        print("OK: .ai-docs/ is up-to-date")
        return 0
    return 1


def _render_readme(
    generated: list[tuple[str, int]],
    guide_data_map: dict[str, GuideData],
    module_descriptions: dict[str, str],
    version: str,
) -> str:
    """Render README.md with version, reading order, and per-module API as Python code snippets."""
    lines: list[str] = []

    # Header
    title = f"# ai-pipeline-core v{version} — API Reference" if version else "# ai-pipeline-core — API Reference"
    lines.extend([
        "<!-- Auto-generated by docs_generator — DO NOT EDIT MANUALLY -->",
        "",
        title,
        "",
        "Auto-generated API reference. Do not edit manually. Run: `make docs-ai-build`",
        "",
        "## Reading Order",
        "",
    ])
    for i, (name, _) in enumerate(generated, 1):
        desc = f" — {module_descriptions[name]}" if name in module_descriptions else ""
        lines.append(f"{i}. [{name}]({name}.md){desc}")

    # Per-module sections
    for name, _ in generated:
        data = guide_data_map.get(name)
        if data:
            _render_module_section(name, data, module_descriptions, lines)

    return "\n".join(lines)


def _render_module_section(
    name: str,
    data: GuideData,
    module_descriptions: dict[str, str],
    lines: list[str],
) -> None:
    """Render a single module section for README.md."""
    lines.extend(["", f"## [{name}]({name}.md)", ""])
    desc = module_descriptions.get(name, "")
    if desc:
        lines.append(desc)

    if data.values:
        lines.append("### Types & Constants")
        lines.append("")
        lines.append("```python")
        for val in data.values:
            source = val.source.strip() if val.source.strip() else val.name
            lines.extend(source.splitlines())
        lines.append("```")
        lines.append("")

    if data.classes:
        lines.append("### Classes")
        lines.append("")
        lines.append("```python")
        for i, cls in enumerate(data.classes):
            if i > 0:
                lines.append("")
                lines.append("")
            _render_class_summary(cls, lines)
        lines.append("```")
        lines.append("")

    if data.functions:
        lines.append("### Functions")
        lines.append("")
        lines.append("```python")
        for i, func in enumerate(data.functions):
            if i > 0:
                lines.append("")
            _render_function_summary(func, lines)
        lines.append("```")
        lines.append("")


def _render_inherited_methods(cls: ClassInfo, lines: list[str]) -> None:
    """Render inherited methods as comment lines inside a class code block."""
    inherited = [m for m in cls.methods if m.is_inherited and is_public_name(m.name)]
    if not inherited:
        return
    lines.append("")
    groups: dict[str, list[str]] = {}
    for m in inherited:
        parent = m.inherited_from or "unknown"
        groups.setdefault(parent, []).append(m.name)
    for parent, names in groups.items():
        lines.append(f"    # Inherited from {parent}: {', '.join(sorted(names))}")


def _render_method_stub(method: MethodInfo, lines: list[str]) -> None:
    """Render a compact method stub for README summaries."""
    prefix = "async " if method.is_async else ""
    lines.append(f"    {prefix}def {method.name}{method.signature}: ...")
    if method.docstring:
        doc_line = method.docstring.splitlines()[0].strip()
        lines.append(f'        """{doc_line}"""')


def _render_class_summary(cls: ClassInfo, lines: list[str]) -> None:
    """Render a class as Python code lines (no code fence — caller wraps)."""
    bases_str = f"({', '.join(cls.bases)})" if cls.bases else ""
    lines.append(f"class {cls.name}{bases_str}:")

    if cls.docstring:
        doc_line = cls.docstring.splitlines()[0].strip()
        lines.append(f'    """{doc_line}"""')

    if cls.class_vars:
        lines.append("")
        lines.append("    # Fields")
        lines.extend(format_class_field(name, ann, default, description) for name, ann, default, description in map(unpack_class_field, cls.class_vars))

    own_methods = [m for m in cls.methods if not m.is_inherited and is_public_name(m.name)]
    if own_methods:
        lines.append("")
        lines.append("    # Methods")
        for method in own_methods:
            if method.is_property:
                lines.append("    @property")
            elif method.is_classmethod:
                lines.append("    @classmethod")
            elif method.is_staticmethod:
                lines.append("    @staticmethod")
            _render_method_stub(method, lines)

    _render_inherited_methods(cls, lines)


def _render_function_summary(func: FunctionInfo, lines: list[str]) -> None:
    """Render a function as Python code for README.md."""
    prefix = "async " if func.is_async else ""
    lines.append(f"{prefix}def {func.name}{func.signature}: ...")
    if func.docstring:
        doc_line = func.docstring.splitlines()[0].strip()
        lines.append(f'    """{doc_line}"""')


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Aggregated validation result across all checks."""

    missing_symbols: tuple[str, ...]
    size_violations: tuple[tuple[str, int], ...]
    private_reexports: tuple[str, ...] = ()

    @property
    def is_valid(self) -> bool:
        """Completeness and private reexport checks must pass. Size is warning-only."""
        return not self.missing_symbols and not self.private_reexports


def validate_completeness(ai_docs_dir: Path, source_dir: Path, excluded_modules: frozenset[str] = frozenset()) -> list[str]:
    """Return public symbols (by naming convention) not found in any guide file."""
    public_symbols = _find_public_symbols(source_dir, excluded_modules)
    guide_content = _read_all_guides(ai_docs_dir)
    return [
        symbol
        for symbol in sorted(public_symbols)
        if not re.search(rf"\bclass {re.escape(symbol)}\b", guide_content)
        and not re.search(rf"\bdef {re.escape(symbol)}\b", guide_content)
        and not re.search(rf"\b{re.escape(symbol)} =", guide_content)
    ]


def validate_size(ai_docs_dir: Path, max_size: int = MAX_GUIDE_SIZE) -> list[tuple[str, int]]:
    """Return guide files exceeding max_size bytes. Skips README.md (separate thresholds)."""
    violations: list[tuple[str, int]] = []
    if not ai_docs_dir.is_dir():
        return violations
    for guide in sorted(ai_docs_dir.glob("*.md")):
        if guide.name == "README.md":
            continue
        size = len(guide.read_bytes())
        if size > max_size:
            violations.append((guide.name, size))
    return violations


def validate_all(
    ai_docs_dir: Path,
    source_dir: Path,
    excluded_modules: frozenset[str] = frozenset(),
) -> ValidationResult:
    """Run all validation checks and return aggregated result."""
    return ValidationResult(
        missing_symbols=tuple(validate_completeness(ai_docs_dir, source_dir, excluded_modules)),
        size_violations=tuple(validate_size(ai_docs_dir)),
        private_reexports=tuple(validate_private_reexports(source_dir, excluded_modules)),
    )


def validate_private_reexports(source_dir: Path, excluded_modules: frozenset[str] = frozenset()) -> list[str]:
    """Detect symbols in __all__ that are imported from private modules.

    Scans every public .py file with __all__ in the package (not just __init__.py).
    For each symbol in __all__, traces the import back to its source module. If the
    source is a _-prefixed module or package, the symbol will appear in IMPORTS but
    have no definition in the guide — a phantom import.

    For non-__init__.py files, symbols that are also exported from the parent
    __init__.py are considered legitimate re-exports (the file is a designated
    public re-export surface like llm/types.py) and are not flagged.
    """
    violations: list[str] = []

    for py_file in sorted(source_dir.rglob("*.py")):
        # Skip _-prefixed files (except __init__.py)
        if py_file.name.startswith("_") and py_file.name != "__init__.py":
            continue
        relative = py_file.relative_to(source_dir)
        # Skip _-prefixed packages (they're entirely private)
        if any(part.startswith("_") for part in relative.parent.parts):
            continue
        # Skip excluded modules
        top_module = relative.parts[0] if len(relative.parts) > 1 else None
        if top_module and top_module in excluded_modules:
            continue

        relative_path = str(relative)

        all_names = _parse_init_all(py_file)
        if not all_names:
            continue

        private_sources = _find_private_import_sources(py_file)

        # For non-__init__.py files, exclude symbols that are also in the parent
        # __init__.py __all__ (they're legitimate re-export surfaces)
        if py_file.name != "__init__.py":
            parent_init = py_file.parent / "__init__.py"
            parent_all = _parse_init_all(parent_init)
            private_only = {k: v for k, v in private_sources.items() if k not in parent_all}
        else:
            private_only = private_sources

        for name in sorted(all_names & private_only.keys()):
            source_module = private_only[name]
            violations.append(
                f"{relative_path}: '{name}' in __all__ is imported from private module '{source_module}'. "
                f"Remove it from __all__ — symbols from _-prefixed modules are internal and won't appear in generated docs."
            )

    return violations


def _parse_init_all(init_file: Path) -> set[str]:
    """Extract __all__ symbol names from a Python file."""
    if not init_file.exists():
        return set()
    try:
        tree = ast.parse(init_file.read_text(encoding="utf-8"))
    except SyntaxError:
        return set()
    for node in tree.body:
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "__all__":
            value = node.value
        if isinstance(value, (ast.List, ast.Tuple)):
            return {elt.value for elt in value.elts if isinstance(elt, ast.Constant) and isinstance(elt.value, str)}
    return set()


def _find_private_import_sources(init_file: Path) -> dict[str, str]:
    """Map symbol names to their private source module for imports from _-prefixed modules.

    Returns {symbol_name: source_module_name} only for symbols imported from private modules.
    """
    try:
        tree = ast.parse(init_file.read_text(encoding="utf-8"))
    except SyntaxError:
        return {}

    result: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        # Check if the import source is a private module
        # Relative: from ._images import ... -> module="_images"
        # Relative nested: from ._llm_core import ... -> module="_llm_core"
        # Absolute: from ai_pipeline_core._llm_core import ... -> contains "_llm_core"
        parts = node.module.split(".")
        is_private = any(part.startswith("_") and part != "__init__" for part in parts)
        if not is_private:
            continue
        # Find the private part for the message
        private_part = next(part for part in parts if part.startswith("_") and part != "__init__")
        for alias in node.names:
            name = alias.asname if alias.asname else alias.name
            result[name] = private_part
    return result


def _find_public_symbols(source_dir: Path, excluded_modules: frozenset[str] = frozenset()) -> set[str]:
    """Find all public symbols via naming convention in non-private modules."""
    symbols: set[str] = set()
    for py_file in sorted(source_dir.rglob("*.py")):
        if py_file.name.startswith("_") and py_file.name != "__init__.py":
            continue
        relative = py_file.relative_to(source_dir)
        top_module = relative.parts[0] if len(relative.parts) > 1 else relative.stem
        if top_module in excluded_modules or (len(relative.parts) > 1 and top_module.startswith("_")):
            continue
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if is_public_name(node.name) and node.name not in _EXCLUDED_SYMBOLS:
                    symbols.add(node.name)
            # NewType / type alias / constant
            elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                if is_public_name(name) and name not in _EXCLUDED_SYMBOLS and ((name.isupper() and len(name) > 1) or _is_newtype_assign(node)):
                    symbols.add(name)
            elif isinstance(node, ast.TypeAlias):
                name = node.name.id
                if is_public_name(name) and name not in _EXCLUDED_SYMBOLS:
                    symbols.add(name)
    return symbols


def _is_newtype_assign(node: ast.Assign) -> bool:
    """Check if an Assign node is a NewType(...) call."""
    if isinstance(node.value, ast.Call):
        func = node.value.func
        if isinstance(func, ast.Name) and func.id == "NewType":
            return True
        if isinstance(func, ast.Attribute) and func.attr == "NewType":
            return True
    return False


def _read_all_guides(ai_docs_dir: Path) -> str:
    """Concatenate all .md guide files into a single string for searching."""
    if not ai_docs_dir.is_dir():
        return ""
    return "\n".join([guide.read_text() for guide in sorted(ai_docs_dir.glob("*.md"))])


def _cli_entry() -> None:
    """Entry point for the ``ai-docs`` console script."""
    raise SystemExit(main())


if __name__ == "__main__":
    raise SystemExit(main())
