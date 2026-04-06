"""Per-module guide assembly with test discovery, relevance scoring, and rendering.

Collects public symbols, resolves dependencies, flattens inheritance,
discovers and scores test examples, extracts rules, and renders guides.
"""

import ast
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from docs_generator.extractor import (
    EXTERNAL_STUBS,
    ClassInfo,
    FunctionInfo,
    MethodInfo,
    SymbolTable,
    ValueInfo,
    format_class_field,
    get_source,
    is_public_name,
    resolve_dependencies,
    unpack_class_field,
)

__all__ = [
    "MAX_EXAMPLES",
    "MAX_GUIDE_SIZE",
    "README_ERROR_SIZE",
    "README_WARN_SIZE",
    "GuideData",
    "ScoredExample",
    "build_guide",
    "discover_tests",
    "flatten_methods",
    "manage_guide_size",
    "render_guide",
    "score_test",
    "select_examples",
]

MAX_EXAMPLES = 8
MAX_GUIDE_SIZE = 40_960  # 40KB in bytes
README_WARN_SIZE = 40_960  # 40KB — warn threshold for README.md
README_ERROR_SIZE = 48_128  # 47KB — error threshold for README.md


def manage_guide_size(
    data: GuideData,
    rendered_content: str,
    max_size: int = MAX_GUIDE_SIZE,
) -> str:
    """Warn if rendered guide exceeds size limit. Returns content unchanged."""
    size = _measure(rendered_content)
    if size <= max_size:
        return rendered_content
    print(
        f"WARNING: {data.module_name} guide is {size:,} bytes ({size // 1024}KB). "
        f"Consider: move private helpers to _ prefixed functions, split large classes into separate modules",
        file=sys.stderr,
    )
    return rendered_content


def _measure(content: str) -> int:
    """Measure guide size in UTF-8 bytes."""
    return len(content.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class ScoredExample:
    """Scored test function extracted for guide examples."""

    name: str
    source_file: str
    line_number: int
    code: str
    score: int
    is_error_example: bool
    is_marked: bool = False


@dataclass
class GuideData:
    """Intermediate representation of a guide, used by trimmer before rendering."""

    module_name: str
    classes: list[ClassInfo]
    functions: list[FunctionInfo]
    external_bases: set[str]
    normal_examples: list[ScoredExample]
    error_examples: list[ScoredExample]
    internal_types: list[ClassInfo] = field(default_factory=list)
    values: list[ValueInfo] = field(default_factory=list)
    purpose: str = ""
    imports: list[str] = field(default_factory=list)
    module_imports: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Test discovery
# ---------------------------------------------------------------------------


def discover_tests(
    module_name: str,
    tests_dir: Path,
    test_dir_overrides: dict[str, str] | None = None,
    repo_root: Path | None = None,
) -> list[ScoredExample]:
    """Find and extract test functions relevant to a module.

    Default mapping: ai_pipeline_core/<module>/ -> tests/<module>/
    Override with test_dir_overrides to remap specific modules.
    Root-level glob tests/test_<module>*.py always runs with original module name.
    """
    test_files: list[Path] = []

    # Subdirectory: use override if provided, else convention
    subdir_name = (test_dir_overrides or {}).get(module_name, module_name)
    subdir = tests_dir / subdir_name
    if subdir.is_dir():
        test_files.extend(sorted(subdir.glob("test_*.py")))

    # Root-level tests always use original module_name
    for f in sorted(tests_dir.glob(f"test_{module_name}*.py")):
        if f not in test_files:
            test_files.append(f)

    examples: list[ScoredExample] = []
    for test_file in test_files:
        examples.extend(_extract_test_functions(test_file, repo_root))
    return examples


# ---------------------------------------------------------------------------
# Scoring and selection
# ---------------------------------------------------------------------------


def score_test(test: ScoredExample, symbol_names: list[str]) -> int:
    """Score a test's relevance to a set of module symbols.

    Returns the best score across all symbols.
    """
    best_score = 0
    test_name_lower = test.name.lower()

    for symbol in symbol_names:
        score = 0
        symbol_lower = symbol.lower()

        # Exact subject match: +5
        subject = test_name_lower.removeprefix("test_")
        if symbol_lower == subject or subject.startswith(symbol_lower + "_"):
            score += 5
        # Partial match: +3
        elif symbol_lower in test_name_lower:
            score += 3

        # Body occurrences: +min(count, 2)
        count = test.code.count(symbol)
        score += min(count, 2)

        best_score = max(best_score, score)

    # Error example bonus: +2
    if test.is_error_example:
        best_score += 2

    # Simplicity bonus
    line_count = len(test.code.splitlines())
    if line_count < 10:
        best_score += 2
    elif line_count < 20:
        best_score += 1

    # Pattern bonus: +1
    if any(p in test.name for p in ("creation", "basic", "simple")):
        best_score += 1

    # Mock penalty — prefer real usage examples over heavily mocked tests
    mock_patterns = ("Mock(", "MagicMock(", "patch(", "monkeypatch.")
    mock_count = sum(test.code.count(p) for p in mock_patterns)
    if mock_count >= 3:
        best_score -= 5
    elif mock_count >= 1:
        best_score -= 2

    # Private API penalty — tests calling ._methods are internal implementation tests
    private_call_count = len(_PRIVATE_CALL_RE.findall(test.code))
    if private_call_count >= 1:
        best_score -= 3

    return max(best_score, 0)


def select_examples(
    tests: list[ScoredExample],
    symbol_names: list[str],
    max_total: int = MAX_EXAMPLES,
) -> tuple[list[ScoredExample], list[ScoredExample]]:
    """Select top examples within budget.

    Marked tests (@pytest.mark.ai_docs) get priority slots.
    Remaining slots filled by top-scored auto tests.
    Returns (normal_examples, error_examples).
    Error cap (max_total // 2) applies to auto tests only.
    If marked tests exceed max_total, all marked tests are included.
    """
    marked = [t for t in tests if t.is_marked]
    auto = [t for t in tests if not t.is_marked]

    scored_auto = sorted(
        [(score_test(t, symbol_names), t) for t in auto],
        key=lambda x: (-x[0], x[1].name),
    )

    marked_normal = [t for t in marked if not t.is_error_example]
    marked_errors = [t for t in marked if t.is_error_example]

    remaining = max(max_total - len(marked), 0)
    if remaining > 0:
        auto_errors = [(s, t) for s, t in scored_auto if t.is_error_example]
        auto_normal = [(s, t) for s, t in scored_auto if not t.is_error_example]

        max_auto_errors = remaining // 2
        selected_auto_errors = [t for _, t in auto_errors[:max_auto_errors]]

        auto_normal_slots = remaining - len(selected_auto_errors)
        selected_auto_normal = [t for _, t in auto_normal[:auto_normal_slots]]
    else:
        selected_auto_errors = []
        selected_auto_normal = []

    return marked_normal + selected_auto_normal, marked_errors + selected_auto_errors


# ---------------------------------------------------------------------------
# Inheritance flattening
# ---------------------------------------------------------------------------


def flatten_methods(cls: ClassInfo, table: SymbolTable) -> tuple[MethodInfo, ...]:
    """Flatten inheritance chain, showing inherited methods inline.

    Uses "child-first, first-match wins" to approximate Python's MRO:
    child methods > left base > right base > grandparent.
    Preserves @overload variants (multiple methods with the same name).
    """
    method_map: dict[str, list[tuple[MethodInfo, str]]] = {}
    visited: set[str] = set()

    def collect(c: ClassInfo) -> None:
        if c.name in visited:
            return
        visited.add(c.name)
        for method in c.methods:
            if method.name not in method_map:
                method_map[method.name] = []
            if not method_map[method.name] or method_map[method.name][0][1] == c.name:
                method_map[method.name].append((method, c.name))
        for base_name in c.bases:
            clean = base_name.split("[")[0]
            if clean in table.classes and clean not in EXTERNAL_STUBS:
                collect(table.classes[clean])

    collect(cls)

    result: list[MethodInfo] = []
    for entries in method_map.values():
        for method, source_class in entries:
            if source_class != cls.name:
                if source_class in EXTERNAL_STUBS:
                    continue
                method = MethodInfo(  # noqa: PLW2901
                    name=method.name,
                    signature=method.signature,
                    docstring=method.docstring,
                    source=method.source,
                    is_property=method.is_property,
                    is_classmethod=method.is_classmethod,
                    is_staticmethod=method.is_staticmethod,
                    is_abstract=method.is_abstract,
                    is_async=method.is_async,
                    line_count=method.line_count,
                    is_inherited=True,
                    inherited_from=source_class,
                )
            result.append(method)

    return tuple(_sort_methods(result))


# ---------------------------------------------------------------------------
# Guide rendering
# ---------------------------------------------------------------------------


def render_guide(data: GuideData, *, version: str = "") -> str:  # noqa: C901, PLR0912, PLR0915
    """Render GuideData to final markdown string with fenced Python code blocks."""
    parts: list[str] = []

    # Header (machine-readable metadata)
    class_names = ", ".join(c.name for c in data.classes)
    external = ", ".join(sorted(data.external_bases))
    parts.append(f"# MODULE: {data.module_name}")
    if class_names:
        parts.append(f"# CLASSES: {class_names}")
    if external:
        parts.append(f"# DEPENDS: {external}")
    if data.purpose:
        parts.append(f"# PURPOSE: {data.purpose}")
    if version:
        parts.append(f"# VERSION: {version}")
    parts.append("# AUTO-GENERATED from source code — do not edit. Run: make docs-ai-build")

    # Imports
    if data.imports or data.module_imports:
        parts.append("")
        parts.append("## Imports")
        parts.append("")
        parts.append("```python")
        if data.imports:
            parts.append(f"from ai_pipeline_core import {', '.join(data.imports)}")
        if data.module_imports:
            parts.append(f"from ai_pipeline_core.{data.module_name} import {', '.join(data.module_imports)}")
        parts.append("```")

    # Types & Constants
    if data.values:
        parts.append("")
        parts.append("## Types & Constants")
        parts.append("")
        parts.append("```python")
        for val in data.values:
            parts.append(val.source)
            parts.append("")
        parts.append("```")

    # Internal types (private classes referenced by public API)
    if data.internal_types:
        parts.append("")
        parts.append("## Internal Types")
        parts.append("")
        parts.append("```python")
        for cls in data.internal_types:
            parts.extend(_render_class(cls))
        parts.append("```")

    # Public API -- classes
    if data.classes:
        parts.append("")
        parts.append("## Public API")
        parts.append("")
        parts.append("```python")
        for cls in data.classes:
            parts.extend(_render_class(cls))
        parts.append("```")

    # Public API -- functions
    if data.functions:
        parts.append("")
        parts.append("## Functions")
        parts.append("")
        parts.append("```python")
        for func in data.functions:
            parts.extend(_render_function(func))
        parts.append("```")

    # Examples
    if data.normal_examples or data.error_examples:
        if data.normal_examples:
            parts.append("")
            parts.append("## Examples")
            parts.append("")
            for ex in data.normal_examples:
                parts.append(f"**{_example_title(ex)}** (`{ex.source_file}:{ex.line_number}`)")
                parts.append("")
                parts.append("```python")
                parts.append(ex.code)
                parts.append("```")
                parts.append("")

        if data.error_examples:
            parts.append("")
            parts.append("## Error Examples")
            parts.append("")
            for ex in data.error_examples:
                parts.append(f"**{_example_title(ex)}** (`{ex.source_file}:{ex.line_number}`)")
                parts.append("")
                parts.append("```python")
                parts.append(ex.code)
                parts.append("```")
                parts.append("")
    else:
        parts.append("")
        parts.append("## Examples")
        parts.append("")
        parts.append("No test examples available.")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def build_guide(  # noqa: PLR0917
    module_name: str,
    source_dir: Path,
    tests_dir: Path,
    table: SymbolTable,
    test_dir_overrides: dict[str, str] | None = None,
    repo_root: Path | None = None,
) -> GuideData:
    """Build guide data for a module.

    Collects public symbols, resolves dependencies, flattens inheritance,
    discovers tests, scores and selects examples.
    """
    if repo_root is None:
        repo_root = source_dir.parent

    public_classes = [c for key, c in table.classes.items() if c.is_public and table.class_to_module.get(key) == module_name]
    public_functions = [f for key, f in table.functions.items() if f.is_public and table.function_to_module.get(key) == module_name]
    public_values = [v for key, v in table.values.items() if v.is_public and table.value_to_module.get(key) == module_name]

    # Resolve dependencies
    root_names = [c.name for c in public_classes]
    _resolved, external_bases = resolve_dependencies(root_names, table)

    # Flatten inheritance for each public class
    flattened_classes: list[ClassInfo] = []
    for cls in public_classes:
        flat_methods = flatten_methods(cls, table)
        flattened_classes.append(
            ClassInfo(
                name=cls.name,
                bases=cls.bases,
                docstring=cls.docstring,
                is_public=cls.is_public,
                class_vars=cls.class_vars,
                methods=flat_methods,
                validators=cls.validators,
                module_path=cls.module_path,
                decorators=cls.decorators,
            )
        )

    # Detect private types referenced by public API signatures
    internal_types = _collect_internal_types(public_functions, flattened_classes, table, module_name)

    # Discover and score tests
    tests = discover_tests(module_name, tests_dir, test_dir_overrides, repo_root)
    if not tests:
        print(f"No tests found for {module_name}", file=sys.stderr)

    symbol_names = root_names + [f.name for f in public_functions]

    for t in tests:
        if t.is_marked and not _has_symbol_overlap(t, symbol_names):
            print(
                f"Marked test {t.name} in {t.source_file} has no symbol overlap with module {module_name}",
                file=sys.stderr,
            )

    example_budget = _compute_example_budget(len(flattened_classes))
    normal_examples, error_examples = select_examples(tests, symbol_names, max_total=example_budget)

    return GuideData(
        module_name=module_name,
        classes=flattened_classes,
        functions=public_functions,
        external_bases=external_bases,
        normal_examples=normal_examples,
        error_examples=error_examples,
        internal_types=internal_types,
        values=public_values,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

BASE_EXAMPLE_BUDGET = 8
MAX_EXAMPLE_BUDGET = 16


def _compute_example_budget(num_classes: int) -> int:
    """Dynamic example budget: more classes get more examples."""
    return min(BASE_EXAMPLE_BUDGET + 2 * max(num_classes - 2, 0), MAX_EXAMPLE_BUDGET)


_PRIVATE_TYPE_RE = re.compile(r"\b_[A-Z]\w*")
_PRIVATE_CALL_RE = re.compile(r"\._(?!_)\w+\(")


def _collect_internal_types(
    public_functions: list[FunctionInfo],
    public_classes: list[ClassInfo],
    table: SymbolTable,
    module_name: str,
) -> list[ClassInfo]:
    """Find private classes from the same module that are referenced in public signatures."""
    # Scan only signatures and type annotations, NOT full function bodies.
    # Body-internal types (e.g. _TraceConfig inside trace()) are implementation details.
    parts: list[str] = [f.signature for f in public_functions]
    for c in public_classes:
        parts.extend(m.signature for m in c.methods if is_public_name(m.name))
        parts.extend(type_ann for _, type_ann, _, _ in c.class_vars if type_ann)
    blob = " ".join(parts)

    referenced = set(_PRIVATE_TYPE_RE.findall(blob))
    if not referenced:
        return []

    return sorted(
        (table.classes[name] for name in referenced if name in table.classes and table.class_to_module.get(name) == module_name),
        key=lambda c: c.name,
    )


def _resolve_class_dependencies(referenced_names: set[str], module_classes: dict[str, ast.ClassDef]) -> set[str]:
    """Transitively resolve class dependencies from referenced names."""
    to_include: set[str] = set()

    def _resolve(name: str) -> None:
        if name in to_include or name not in module_classes:
            return
        to_include.add(name)
        cls_node = module_classes[name]
        for child in ast.walk(cls_node):
            if isinstance(child, ast.Name) and child.id in module_classes and child.id != name:
                _resolve(child.id)

    for name in referenced_names:
        if name in module_classes:
            _resolve(name)

    return to_include


def _collect_module_context(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    tree: ast.Module,
    source_lines: list[str],
) -> str:
    """Collect module-level class definitions referenced by a marked test function.

    Scans the function body for Name references, finds matching top-level ClassDef
    nodes in the same file, and transitively resolves base classes and type annotation
    dependencies. Returns their source in file-definition order.
    """
    referenced_names: set[str] = set()
    for node in ast.walk(func_node):
        if isinstance(node, ast.Name):
            referenced_names.add(node.id)

    module_classes: dict[str, ast.ClassDef] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and not node.name.startswith("Test"):
            module_classes[node.name] = node

    if not module_classes:
        return ""

    to_include = _resolve_class_dependencies(referenced_names, module_classes)
    if not to_include:
        return ""

    parts = [get_source(source_lines, node) for node in tree.body if isinstance(node, ast.ClassDef) and node.name in to_include]
    return "\n\n\n".join(parts)


def _extract_test_functions(path: Path, repo_root: Path | None = None) -> list[ScoredExample]:
    source = path.read_text(encoding="utf-8")
    source_lines = source.splitlines()
    tree = ast.parse(source)

    if repo_root is not None:
        try:
            source_file = str(path.relative_to(repo_root))
        except ValueError:
            source_file = str(path)
    else:
        source_file = str(path)

    results: list[ScoredExample] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue

        code = get_source(source_lines, node)
        code = "\n".join(line for line in code.splitlines() if "pytest.mark.ai_docs" not in line and "mark.ai_docs" not in line)
        code = "\n".join(_dedented_source(code))

        is_marked = _has_ai_docs_marker(node)

        if is_marked:
            context = _collect_module_context(node, tree, source_lines)
            if context:
                code = context + "\n\n\n" + code

        results.append(
            ScoredExample(
                name=node.name,
                source_file=source_file,
                line_number=node.lineno,
                code=code,
                score=0,
                is_error_example=_uses_pytest_raises(node),
                is_marked=is_marked,
            )
        )
    return results


def _uses_pytest_raises(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute) and child.attr == "raises" and isinstance(child.value, ast.Name) and child.value.id == "pytest":
            return True
    return False


def _has_ai_docs_marker(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Check if a test function has @pytest.mark.ai_docs decorator."""
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if not isinstance(target, ast.Attribute) or target.attr != "ai_docs":
            continue
        # pytest.mark.ai_docs
        if (
            isinstance(target.value, ast.Attribute)
            and target.value.attr == "mark"
            and isinstance(target.value.value, ast.Name)
            and target.value.value.id == "pytest"
        ):
            return True
        # mark.ai_docs (from pytest import mark)
        if isinstance(target.value, ast.Name) and target.value.id == "mark":
            return True
    return False


def _has_symbol_overlap(test: ScoredExample, symbol_names: list[str]) -> bool:
    """Check if a test references any of the given symbol names."""
    return any(symbol.lower() in test.name.lower() or symbol in test.code for symbol in symbol_names)


def _sort_methods(methods: list[MethodInfo]) -> list[MethodInfo]:
    """Sort: __init__ first, then properties, classmethods, regular methods."""

    def key(m: MethodInfo) -> tuple[int, str]:
        if m.name == "__init__":
            return (0, m.name)
        if m.is_property:
            return (1, m.name)
        if m.is_classmethod:
            return (2, m.name)
        return (3, m.name)

    return sorted(methods, key=key)


def _render_class(cls: ClassInfo) -> list[str]:
    lines: list[str] = []
    if any(b.split("[")[0] == "Protocol" for b in cls.bases):
        lines.append("# Protocol — implement in concrete class")
    elif any(b.split("[")[0] in {"StrEnum", "Enum", "IntEnum"} for b in cls.bases):
        lines.append("# Enum")
    lines.extend(f"@{dec}" for dec in cls.decorators)
    bases_str = f"({', '.join(cls.bases)})" if cls.bases else ""
    lines.append(f"class {cls.name}{bases_str}:")

    if cls.docstring:
        lines.append(f'    """{cls.docstring.strip()}"""')

    for class_field in cls.class_vars:
        var_name, type_ann, default, description = unpack_class_field(class_field)
        lines.append(format_class_field(var_name, type_ann, default, description))

    if cls.class_vars:
        lines.append("")

    # Separate own methods from inherited
    own_methods = [m for m in cls.methods if not m.is_inherited and is_public_name(m.name)]
    inherited_methods = [m for m in cls.methods if m.is_inherited and is_public_name(m.name)]

    # Render inherited methods as compact grouped references
    if inherited_methods:
        groups: dict[str, list[str]] = {}
        for m in inherited_methods:
            parent = m.inherited_from or "unknown"
            groups.setdefault(parent, []).append(m.name)
        for parent, names in groups.items():
            lines.append(f"    # [Inherited from {parent}]")
            lines.append(f"    # {', '.join(sorted(names))}")
        lines.append("")

    # Render own methods with full source
    for method in own_methods:
        lines.extend(_render_method(method))

    lines.append("")
    return lines


def _render_method(method: MethodInfo) -> list[str]:
    lines: list[str] = [f"    {source_line}" for source_line in _dedented_source(method.source)]
    lines.append("")
    return lines


def _render_function(func: FunctionInfo) -> list[str]:
    lines: list[str] = list(_dedented_source(func.source))
    lines.append("")
    return lines


def _dedented_source(source: str) -> list[str]:
    """Dedent source based on first line's indentation.

    Uses the first non-empty line's indentation as the base level,
    which correctly handles multi-line strings with content at column 0.
    """
    raw_lines = source.splitlines()
    if not raw_lines:
        return []
    first_non_empty = next((line for line in raw_lines if line.strip()), None)
    if first_non_empty is None:
        return raw_lines
    indent = len(first_non_empty) - len(first_non_empty.lstrip())
    if indent == 0:
        return raw_lines
    return [line[indent:] if line[:indent].isspace() else line for line in raw_lines]


def _example_title(ex: ScoredExample) -> str:
    """Convert test function name to readable title."""
    return ex.name.removeprefix("test_").replace("_", " ").capitalize()
