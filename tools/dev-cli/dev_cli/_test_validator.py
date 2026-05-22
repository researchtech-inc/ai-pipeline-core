"""AST validator for test authoring policies."""

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml import YAML

ALLOWED_TEST_SUBDIRS = frozenset({"unit", "integration", "qualification", "capabilities", "support"})
TEST_LANES = frozenset({"unit", "integration", "qualification", "capabilities"})
SLEEP_CHECK_LANES = frozenset({"unit", "integration"})
LIVE_CALL_ATTRS = frozenset({"send", "send_structured"})


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


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _read_tree(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        line = exc.lineno or 1
        raise ValueError(f"{path}:{line}: syntax error: {exc.msg}") from exc


def _lane(path: Path, root: Path) -> str | None:
    try:
        rel = path.relative_to(root / "tests")
    except ValueError:
        return None
    parts = rel.parts
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
    if not _is_monkeypatch_setattr_call(node):
        return False
    if not node.args:
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
        if not isinstance(call, ast.Call):
            continue
        func_name = _module_name(call.func)
        if func_name == "pytest.raises":
            return True
    return False


def _call_line(node: ast.AST) -> int:
    return getattr(node, "lineno", 1)


def _contains_live_call(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        if isinstance(child.func, ast.Attribute) and child.func.attr in LIVE_CALL_ATTRS:
            return True
    return False


def _check_live_calls_in_function(path: Path, function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[Violation]:
    arg_names = {arg.arg for arg in (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)}
    if "transport_spy" in arg_names:
        return []

    violations: list[Violation] = []
    patched = False

    for stmt in function.body:
        if any(_patches_transport_or_generation(child) for child in ast.walk(stmt)):
            patched = True
        if _is_pytest_raises_context(stmt) and _contains_live_call(stmt):
            continue
        if patched:
            continue
        for child in ast.walk(stmt):
            if not isinstance(child, ast.Call):
                continue
            if isinstance(child.func, ast.Attribute) and child.func.attr in LIVE_CALL_ATTRS:
                violations.append(
                    Violation(
                        path=path,
                        line=_call_line(child),
                        rule_id="A",
                        message="unit test Conversation.send/send_structured must use transport_spy or patch the transport/generation boundary before the call",
                    )
                )
    return violations


def _check_live_call_escape(path: Path, tree: ast.Module, root: Path) -> list[Violation]:
    if _lane(path, root) != "unit":
        return []
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test_"):
            violations.extend(_check_live_calls_in_function(path, node))
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

    data = YAML(typ="safe").load(manifest.read_text(encoding="utf-8"))
    raw_entries = data.get("showcases", []) if isinstance(data, dict) else []
    declared = {entry["path"] for entry in raw_entries if isinstance(entry, dict) and isinstance(entry.get("path"), str)}
    examples_dir = root / "examples"
    on_disk = {path.relative_to(root).as_posix() for path in examples_dir.glob("*.py")}

    violations: list[Violation] = []
    for missing in sorted(on_disk - declared):
        violations.append(
            Violation(
                path=root / missing,
                line=1,
                rule_id="E",
                message="showcase missing from examples/manifest.yml",
            )
        )
    for stale in sorted(declared - on_disk):
        violations.append(
            Violation(
                path=root / stale,
                line=1,
                rule_id="E",
                message="manifest entry points at non-existent file",
            )
        )
    return violations


def _literal_number(node: ast.AST) -> float | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        value = _literal_number(node.operand)
        return -value if value is not None else None
    return None


def _module_constants(tree: ast.Module) -> dict[str, float]:
    constants: dict[str, float] = {}
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign):
            continue
        value = _literal_number(stmt.value)
        if value is None:
            continue
        for target in stmt.targets:
            if isinstance(target, ast.Name):
                constants[target.id] = value
    return constants


def _sleep_argument_value(arg: ast.AST, constants: dict[str, float]) -> float | None:
    literal = _literal_number(arg)
    if literal is not None:
        return literal
    if isinstance(arg, ast.Name):
        return constants.get(arg.id)
    return None


def _is_asyncio_sleep(call: ast.Call) -> bool:
    return isinstance(call.func, ast.Attribute) and call.func.attr == "sleep" and _module_name(call.func.value) == "asyncio"


def _check_sleep_constants(path: Path, tree: ast.Module, root: Path) -> list[Violation]:
    if _lane(path, root) not in SLEEP_CHECK_LANES:
        return []
    constants = _module_constants(tree)
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Await) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if not _is_asyncio_sleep(call) or not call.args:
            continue
        resolved = _sleep_argument_value(call.args[0], constants)
        if resolved is not None and resolved >= 1:
            violations.append(
                Violation(
                    path=path,
                    line=getattr(call, "lineno", 1),
                    rule_id="F",
                    message="asyncio.sleep() resolved to a >=1 second constant outside the qualification lane",
                )
            )
    return violations


def validate(root: Path) -> list[Violation]:
    """Run all validator checks."""
    violations: list[Violation] = []
    violations.extend(_check_lane_directories(root))
    violations.extend(_check_examples_manifest(root))

    for path in sorted((root / "tests").rglob("test_*.py")):
        tree = _read_tree(path)
        if tree is None:
            continue
        violations.extend(_check_live_call_escape(path, tree, root))
        violations.extend(_check_cross_lane_imports(path, tree, root))
        violations.extend(_check_qualification_docstring(path, tree, root))
        violations.extend(_check_sleep_constants(path, tree, root))
    return violations


def main() -> int:
    """Validate the repository and print any violations."""
    root = _repo_root()
    try:
        violations = validate(root)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for violation in violations:
        print(violation.format(root))
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
