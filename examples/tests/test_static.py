"""Static checks for repository examples."""

import ast
import importlib
import re
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = ROOT / "examples"
MANIFEST_PATH = EXAMPLES_DIR / "manifest.yml"

FORBIDDEN_MODEL_PATTERNS = (
    re.compile(r"Conversation\(model\s*=\s*['\"]"),
    re.compile(r"\bcore_model\s*:\s*str\b"),
    re.compile(r"\bfast_model\s*:\s*str\b"),
    re.compile(r"\bmodel\s*:\s*str\b"),
    re.compile(r"\bmodel\s*=\s*['\"]"),
)
SEND_METHODS = frozenset({"send", "send_structured", "send_spec"})
BANNED_FULL_CALLS = frozenset({
    "time.sleep",
    "asyncio.sleep",
    "Conversation.send",
    "Conversation.send_structured",
    "Conversation.send_spec",
    "PipelineDeployment.run",
})
BANNED_CALL_NAMES = frozenset({"generate", "execute_interaction"})
DEPLOYMENT_BASES = frozenset({"PipelineDeployment", "RemoteDeployment"})


def _manifest() -> dict[str, Any]:
    data = YAML(typ="safe").load(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _showcase_entries() -> tuple[dict[str, Any], ...]:
    entries = _manifest().get("showcases", ())
    assert isinstance(entries, list)
    for entry in entries:
        assert isinstance(entry, dict)
    return tuple(entries)


def _entry_id(entry: dict[str, Any]) -> str:
    path = entry.get("path")
    assert isinstance(path, str)
    return path


def _module_name_from_path(path: str) -> str:
    return Path(path).with_suffix("").as_posix().replace("/", ".")


def _expr_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _expr_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Call):
        return _expr_name(node.func)
    if isinstance(node, ast.Subscript):
        return _expr_name(node.value)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def _leaf_name(node: ast.AST) -> str:
    return _expr_name(node).rsplit(".", 1)[-1]


def _is_main_guard(node: ast.AST) -> bool:
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or len(test.comparators) != 1:
        return False
    if not isinstance(test.ops[0], ast.Eq):
        return False
    left = _expr_name(test.left)
    right = _expr_name(test.comparators[0])
    return (left, right) in {("__name__", "__main__"), ("__main__", "__name__")}


def _deployment_class_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if any(_leaf_name(base) in DEPLOYMENT_BASES for base in node.bases):
            names.add(node.name)
    return names


def _assigned_names(target: ast.AST) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Tuple | ast.List):
        return {name for item in target.elts for name in _assigned_names(item)}
    return set()


def _module_objects(tree: ast.Module, class_names: set[str], factory_name: str) -> set[str]:
    names: set[str] = set()
    for node in ast.iter_child_nodes(tree):
        if _is_main_guard(node):
            continue
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and _leaf_name(node.value.func) in class_names:
            for target in node.targets:
                names.update(_assigned_names(target))
        if isinstance(node, ast.AnnAssign) and isinstance(node.value, ast.Call) and _leaf_name(node.value.func) in class_names:
            names.update(_assigned_names(node.target))
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and _leaf_name(node.value.func) == factory_name:
            for target in node.targets:
                names.update(_assigned_names(target))
        if isinstance(node, ast.AnnAssign) and isinstance(node.value, ast.Call) and _leaf_name(node.value.func) == factory_name:
            names.update(_assigned_names(node.target))
    return names


def _is_deployment_run(call: ast.Call, deployment_objects: set[str], deployment_classes: set[str]) -> bool:
    if not isinstance(call.func, ast.Attribute) or call.func.attr != "run":
        return False
    target = call.func.value
    target_name = _expr_name(target)
    if target_name in deployment_objects or target_name == "PipelineDeployment":
        return True
    return isinstance(target, ast.Call) and _leaf_name(target.func) in deployment_classes


def _is_conversation_send(call: ast.Call, conversation_objects: set[str]) -> bool:
    if not isinstance(call.func, ast.Attribute) or call.func.attr not in SEND_METHODS:
        return False
    target = call.func.value
    target_name = _expr_name(target)
    if target_name in conversation_objects:
        return True
    return isinstance(target, ast.Call) and _leaf_name(target.func) == "Conversation"


def _call_violation(call: ast.Call, deployment_objects: set[str], deployment_classes: set[str], conversation_objects: set[str]) -> str | None:
    name = _expr_name(call.func)
    leaf = name.rsplit(".", 1)[-1]
    if name in BANNED_FULL_CALLS or leaf in BANNED_CALL_NAMES:
        return name
    if _is_conversation_send(call, conversation_objects):
        return name
    if _is_deployment_run(call, deployment_objects, deployment_classes):
        return name
    return None


def _scan_import_time_node(
    node: ast.AST,
    deployment_objects: set[str],
    deployment_classes: set[str],
    conversation_objects: set[str],
) -> list[str]:
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
        return []
    if _is_main_guard(node):
        return []

    violations: list[str] = []
    if isinstance(node, ast.Call):
        banned = _call_violation(node, deployment_objects, deployment_classes, conversation_objects)
        if banned is not None:
            violations.append(f"{getattr(node, 'lineno', 1)}: {banned}")
    for child in ast.iter_child_nodes(node):
        violations.extend(_scan_import_time_node(child, deployment_objects, deployment_classes, conversation_objects))
    return violations


def _module_scope_violations(tree: ast.Module) -> list[str]:
    deployment_classes = _deployment_class_names(tree)
    deployment_objects = _module_objects(tree, deployment_classes, "")
    conversation_objects = _module_objects(tree, {"Conversation"}, "Conversation")
    violations: list[str] = []
    for node in ast.iter_child_nodes(tree):
        violations.extend(_scan_import_time_node(node, deployment_objects, deployment_classes, conversation_objects))
    return violations


def test_manifest_covers_top_level_showcases() -> None:
    declared = {_entry_id(entry) for entry in _showcase_entries()}
    actual = {path.relative_to(ROOT).as_posix() for path in EXAMPLES_DIR.glob("*.py")}
    missing = sorted(actual - declared)
    stale = sorted(declared - actual)
    assert missing == []
    assert stale == []


@pytest.mark.parametrize("entry", _showcase_entries(), ids=_entry_id)
def test_showcase_static_manifest_entry(entry: dict[str, Any]) -> None:
    path_text = _entry_id(entry)
    path = ROOT / path_text
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=path_text)

    importlib.import_module(_module_name_from_path(path_text))

    module_scope_violations = _module_scope_violations(tree)
    assert module_scope_violations == []


@pytest.mark.parametrize("entry", _showcase_entries(), ids=_entry_id)
def test_examples_do_not_use_string_model_patterns(entry: dict[str, Any]) -> None:
    path_text = _entry_id(entry)
    text = (ROOT / path_text).read_text(encoding="utf-8")
    offenders = [f"{path_text}: {pattern.pattern}" for pattern in FORBIDDEN_MODEL_PATTERNS if pattern.search(text)]
    assert offenders == []
