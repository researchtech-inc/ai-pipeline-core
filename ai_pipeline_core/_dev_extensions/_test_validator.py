"""AI Pipeline Core test authoring validator."""

import ast
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from ruamel.yaml import YAML

ALLOWED_TEST_SUBDIRS = frozenset({"unit", "integration", "qualification", "capabilities", "support"})
TEST_LANES = frozenset({"unit", "integration", "qualification", "capabilities"})
CONVERSATION_SEND_ATTRS = frozenset({"send", "send_structured"})
CONVERSATION_SEND_PATCH_BOUNDARY_MESSAGE = " ".join((
    "unit test Conversation.send/send_structured must use transport_spy or patch the",
    "transport/generation boundary before the call",
))
ROOT_MARKERS = ("pyproject.toml", ".git")

HARDCODED_MODEL_CALLEES = frozenset({"AIModel", "Conversation"})
HARDCODED_MODEL_EXCLUDE = frozenset({
    "tests/support/model_catalog.py",
    "benchmarks/llm_matrix/_models.py",
    "benchmarks/llm_matrix/_inventory.py",
    "tests/qualification/compatibility/_models.py",
})
BULK_MODEL_CONSTANT_NAMES = frozenset({"ALL_MODELS", "CORE_MODELS", "SEARCH_MODELS"})
BULK_MODEL_CONSTANT_EXCLUDE_PREFIXES = ("benchmarks/", "tests/qualification/compatibility/")
PROJECT_SCAN_ROOTS = ("tests", "benchmarks")


@dataclass(frozen=True, slots=True)
class Violation:
    """One validator violation."""

    path: Path
    line: int
    rule_id: str
    message: str

    def format(self, root: Path) -> str:
        """Return the stable validator output line."""
        return f"{self.path.relative_to(root).as_posix()}:{self.line}: VALIDATOR-{self.rule_id}: {self.message}"


def _repo_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        if any((directory / marker).exists() for marker in ROOT_MARKERS):
            return directory
    return current


def _read_tree(path: Path) -> ast.Module:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        line = exc.lineno or 1
        raise ValueError(f"{path}:{line}: syntax error: {exc.msg}") from exc


def _lane(path: Path, root: Path) -> str | None:
    try:
        relative = path.relative_to(root / "tests")
    except ValueError:
        return None
    parts = relative.parts
    if not parts:
        return None
    return parts[0] if parts[0] in TEST_LANES else None


def _module_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _module_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def _is_monkeypatch_setattr_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "setattr"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "monkeypatch"
    )


def _patches_transport_or_generation(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or not _is_monkeypatch_setattr_call(node) or not node.args:
        return False

    target = _module_name(node.args[0])
    attr = _module_name(node.args[1]) if len(node.args) > 1 else ""
    if target in {"_transport", "ai_pipeline_core._llm_core._transport"} and attr == "open_stream":
        return True
    if target == "ai_pipeline_core._llm_core._transport.open_stream":
        return True
    return target == "ai_pipeline_core.llm._engine.generate"


def _is_pytest_raises_context(node: ast.AST) -> bool:
    if not isinstance(node, ast.With):
        return False
    for item in node.items:
        call = item.context_expr
        if isinstance(call, ast.Call) and _module_name(call.func) == "pytest.raises":
            return True
    return False


def _is_conversation_send_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    return node.func.attr in CONVERSATION_SEND_ATTRS


def _contains_conversation_send_call(node: ast.AST) -> bool:
    return any(_is_conversation_send_call(child) for child in ast.walk(node))


def _check_conversation_send_in_function(
    path: Path,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[Violation]:
    arg_names = {arg.arg for arg in (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)}
    if "transport_spy" in arg_names:
        return []

    violations: list[Violation] = []
    patched = False
    for statement in function.body:
        if any(_patches_transport_or_generation(child) for child in ast.walk(statement)):
            patched = True
        if _is_pytest_raises_context(statement) and _contains_conversation_send_call(statement):
            continue
        if patched:
            continue
        violations.extend(_conversation_send_violations(path, statement))
    return violations


def _conversation_send_violations(path: Path, node: ast.AST) -> list[Violation]:
    violations: list[Violation] = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        if isinstance(child.func, ast.Attribute) and child.func.attr in CONVERSATION_SEND_ATTRS:
            violations.append(
                Violation(
                    path=path,
                    line=getattr(child, "lineno", 1),
                    rule_id="A",
                    message=CONVERSATION_SEND_PATCH_BOUNDARY_MESSAGE,
                )
            )
    return violations


def _check_conversation_send_escape(path: Path, tree: ast.Module, root: Path) -> list[Violation]:
    if _lane(path, root) != "unit":
        return []

    violations: list[Violation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test_"):
            violations.extend(_check_conversation_send_in_function(path, node))
    return violations


def _imported_module(node: ast.AST) -> str | None:
    if isinstance(node, ast.ImportFrom):
        return node.module
    if isinstance(node, ast.Import):
        return node.names[0].name if node.names else None
    return None


def _check_cross_lane_imports(path: Path, tree: ast.Module, root: Path) -> list[Violation]:
    lane = _lane(path, root)
    if lane not in {"unit", "integration"}:
        return []

    forbidden = ("tests.integration", "tests.qualification") if lane == "unit" else ("tests.qualification",)
    violations: list[Violation] = []
    for node in ast.walk(tree):
        module = _imported_module(node)
        if module is None:
            continue
        if any(module == item or module.startswith(f"{item}.") for item in forbidden):
            violations.append(
                Violation(
                    path=path,
                    line=getattr(node, "lineno", 1),
                    rule_id="B",
                    message=f"{lane} tests must not import from higher-cost test lanes",
                )
            )
    return violations


def _string_keyword_argument(keyword: ast.keyword) -> bool:
    return (
        keyword.arg in {"name", "model"}
        and isinstance(keyword.value, ast.Constant)
        and isinstance(keyword.value.value, str)
    )


def _hardcoded_model_call(call: ast.Call) -> bool:
    name = _module_name(call.func)
    if name not in HARDCODED_MODEL_CALLEES:
        return False
    if any(_string_keyword_argument(keyword) for keyword in call.keywords):
        return True
    return bool(call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str))


def _check_hardcoded_models(path: Path, tree: ast.Module, root: Path) -> list[Violation]:
    if _relative_path(root, path) in HARDCODED_MODEL_EXCLUDE:
        return []
    return [
        Violation(
            path=path,
            line=getattr(node, "lineno", 1),
            rule_id="G",
            message=(
                "tests must get models from tests/support/model_catalog.py; "
                "use a provider-agnostic fixture or catalog constant"
            ),
        )
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _hardcoded_model_call(node)
    ]


def _check_bulk_model_constants(path: Path, tree: ast.Module, root: Path) -> list[Violation]:
    relative = _relative_path(root, path)
    if any(relative.startswith(prefix) for prefix in BULK_MODEL_CONSTANT_EXCLUDE_PREFIXES):
        return []
    return [
        Violation(
            path=path,
            line=getattr(node, "lineno", 1),
            rule_id="H",
            message=(
                "bulk model constants are for benchmark and compatibility matrices only; "
                "use small capability fixtures from tests/support/model_catalog.py"
            ),
        )
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id in BULK_MODEL_CONSTANT_NAMES
    ]


def _relative_path(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _check_qualification_docstring(path: Path, tree: ast.Module, root: Path) -> list[Violation]:
    if _lane(path, root) != "qualification":
        return []

    docstring = ast.get_docstring(tree) or ""
    lowered = docstring.lower()
    if docstring.strip() and ("qualification" in lowered or "lane:" in lowered):
        return []

    return [
        Violation(
            path=path,
            line=1,
            rule_id="C",
            message="qualification test modules need a docstring containing 'qualification' or a 'lane:' justification",
        )
    ]


def _check_lane_directories(root: Path) -> list[Violation]:
    tests_dir = root / "tests"
    violations: list[Violation] = []
    if not tests_dir.is_dir():
        return violations

    for child in sorted(tests_dir.iterdir()):
        if child.name.startswith("__pycache__"):
            continue
        if child.is_dir() and child.name not in ALLOWED_TEST_SUBDIRS:
            violations.append(
                Violation(
                    path=child,
                    line=1,
                    rule_id="D",
                    message=f"immediate tests/ subdirectories must be one of {sorted(ALLOWED_TEST_SUBDIRS)}",
                )
            )

    for test_file in tests_dir.rglob("test_*.py"):
        lane = _lane(test_file, root)
        if lane not in TEST_LANES:
            violations.append(
                Violation(
                    path=test_file,
                    line=1,
                    rule_id="D",
                    message="test_*.py files must live under tests/unit/, tests/integration/, or tests/qualification/",
                )
            )
    return violations


def _check_examples_manifest(root: Path) -> list[Violation]:
    manifest = root / "examples" / "manifest.yml"
    if not manifest.exists():
        return [
            Violation(
                path=manifest,
                line=1,
                rule_id="E",
                message="examples/manifest.yml is required",
            )
        ]

    data: object = YAML(typ="safe").load(  # type: ignore[reportUnknownMemberType]  # ruamel load is untyped.
        manifest.read_text(encoding="utf-8")
    )
    data_mapping = cast(Mapping[object, object], data) if isinstance(data, Mapping) else {}
    raw_entries = data_mapping.get("showcases", ())
    declared = _declared_showcase_paths(raw_entries)
    examples_dir = root / "examples"
    on_disk = {path.relative_to(root).as_posix() for path in examples_dir.glob("*.py")}

    violations = [
        Violation(
            path=root / missing,
            line=1,
            rule_id="E",
            message="showcase missing from examples/manifest.yml",
        )
        for missing in sorted(on_disk - declared)
    ]
    violations.extend(
        Violation(
            path=root / stale,
            line=1,
            rule_id="E",
            message="manifest entry points at non-existent file",
        )
        for stale in sorted(declared - on_disk)
    )
    return violations


def _declared_showcase_paths(raw_entries: object) -> set[str]:
    if not isinstance(raw_entries, list | tuple):
        return set()

    declared: set[str] = set()
    for entry in raw_entries:
        if not isinstance(entry, Mapping):
            continue
        entry_mapping = cast(Mapping[object, object], entry)
        path = entry_mapping.get("path")
        if isinstance(path, str):
            declared.add(path)
    return declared


def _iter_scanned_files(root: Path) -> list[Path]:
    files: set[Path] = set()
    for scope in PROJECT_SCAN_ROOTS:
        scope_dir = root / scope
        if not scope_dir.is_dir():
            continue
        files.update(path for path in scope_dir.rglob("*.py") if path.is_file())
    return sorted(files)


def validate(root: Path) -> list[Violation]:
    """Run all project-owned test authoring checks."""
    violations: list[Violation] = []
    violations.extend(_check_lane_directories(root))
    violations.extend(_check_examples_manifest(root))

    for path in _iter_scanned_files(root):
        tree = _read_tree(path)
        if path.name.startswith("test_"):
            violations.extend(_check_conversation_send_escape(path, tree, root))
            violations.extend(_check_cross_lane_imports(path, tree, root))
            violations.extend(_check_qualification_docstring(path, tree, root))
        violations.extend(_check_hardcoded_models(path, tree, root))
        violations.extend(_check_bulk_model_constants(path, tree, root))

    return sorted(violations, key=lambda item: (_relative_path(root, item.path), item.line, item.rule_id))


def main() -> int:
    """Validate the repository and print any violations."""
    root = _repo_root()
    try:
        violations = validate(root)
    except ValueError as exc:
        sys.stderr.write(f"{exc}\n")
        sys.stderr.write("Next step: fix the syntax error and rerun the project test validator.\n")
        return 1

    for violation in violations:
        sys.stdout.write(f"{violation.format(root)}\n")
    if violations:
        sys.stdout.write("Next step: fix the validator violations and rerun the project test validator.\n")
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
