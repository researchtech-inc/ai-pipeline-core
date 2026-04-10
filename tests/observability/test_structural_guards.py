"""Structural regression guards for 0.22.0 observability changes.

Each test asserts a structural invariant the code must continue to satisfy.
These are not behavior tests; they protect against silent regression of
decisions made in the 0.22.0 observability redesign. If a decision is
intentionally revisited, the corresponding assertion must be updated with
an explanation.
"""

import ast
import importlib.util
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_AI_PIPELINE_CORE = _PROJECT_ROOT / "ai_pipeline_core"
_RECOVERY_MODULE_PATH = _AI_PIPELINE_CORE / "observability" / "_recovery.py"
_SENTRY_SINK_PATH = _AI_PIPELINE_CORE / "observability" / "_sentry_sink.py"
_DEPLOYMENT_TYPES_PATH = _AI_PIPELINE_CORE / "deployment" / "_types.py"
_OBSERVABILITY_CLI_PATH = _AI_PIPELINE_CORE / "observability" / "cli.py"


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _assigned_names(module: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in module.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _calls_to(module: ast.Module, dotted_name: str) -> list[ast.Call]:
    """Return all Call nodes that invoke ``dotted_name`` (e.g. 'typing.cast')."""
    head, _, tail = dotted_name.partition(".")
    results: list[ast.Call] = []
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if tail:
            if isinstance(func, ast.Attribute) and func.attr == tail and isinstance(func.value, ast.Name) and func.value.id == head:
                results.append(node)
        elif isinstance(func, ast.Name) and func.id == head:
            results.append(node)
    return results


def _string_arg(call: ast.Call, index: int) -> str | None:
    if index >= len(call.args):
        return None
    node = call.args[index]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _has_name_in_args(call: ast.Call, name: str) -> bool:
    return any(isinstance(arg, ast.Name) and arg.id == name for arg in call.args)


def test_recovery_module_file_still_exists() -> None:
    """Item 11: the rename to `_reconcile.py` is deferred to 0.23.0."""
    assert _RECOVERY_MODULE_PATH.exists(), (
        "ai_pipeline_core/observability/_recovery.py must exist in 0.22.x. "
        "The rename to `_reconcile.py` is deferred to 0.23.0; update this test "
        "when the rename lands and downstream consumers have migrated."
    )


def test_recovery_module_does_not_redefine_crash_error_type() -> None:
    """Item 12: `_CRASH_ERROR_TYPE` lives in `deployment/_types.py` only."""
    module = _parse(_RECOVERY_MODULE_PATH)
    assert "_CRASH_ERROR_TYPE" not in _assigned_names(module), (
        "`_CRASH_ERROR_TYPE` must only be defined in `ai_pipeline_core/deployment/_types.py`. `observability/_recovery.py` must import it, not redefine it."
    )
    types_module = _parse(_DEPLOYMENT_TYPES_PATH)
    assert "_CRASH_ERROR_TYPE" in _assigned_names(types_module), "`_CRASH_ERROR_TYPE` must be defined in `ai_pipeline_core/deployment/_types.py`."


def test_recovery_has_module_level_prefect_import() -> None:
    """Item 13: dynamic `importlib.import_module` for Prefect is banned."""
    module = _parse(_RECOVERY_MODULE_PATH)
    has_direct_import = any(
        isinstance(node, ast.ImportFrom) and node.module == "prefect.exceptions" and any(alias.name == "ObjectNotFound" for alias in node.names)
        for node in module.body
    )
    assert has_direct_import, (
        "`observability/_recovery.py` must use a module-level `from prefect.exceptions import ObjectNotFound`. "
        "Item 13 of the 0.22.0 observability plan bans dynamic `importlib.import_module` for Prefect."
    )

    for call in _calls_to(module, "importlib.import_module"):
        value = _string_arg(call, 0)
        assert value is None or not value.startswith("prefect"), (
            "`observability/_recovery.py` must not import Prefect modules via `importlib.import_module`. "
            f"Found call with argument {value!r}. Use a top-level `from prefect... import ...` instead."
        )


def test_observability_cli_has_module_level_prefect_import() -> None:
    """Item 13: `cli.py` must also import Prefect at module level."""
    module = _parse(_OBSERVABILITY_CLI_PATH)
    for call in _calls_to(module, "importlib.import_module"):
        value = _string_arg(call, 0)
        assert value is None or not value.startswith("prefect"), (
            "`observability/cli.py` must not import Prefect via `importlib.import_module`. "
            f"Found call with argument {value!r}. Use a top-level `from prefect... import ...` instead."
        )


def test_sentry_sink_has_no_cast_any() -> None:
    """Item 2: the Sentry sink must rely on the local type stub, not `cast(Any, ...)`."""
    module = _parse(_SENTRY_SINK_PATH)
    for call in _calls_to(module, "cast"):
        if _has_name_in_args(call, "Any"):
            raise AssertionError(
                "`observability/_sentry_sink.py` must not use `cast(Any, ...)`. "
                "Item 2 of the 0.22.0 observability plan eradicated this pattern in favor "
                "of the local `sentry_sdk/__init__.pyi` type stub. Extend the stub instead."
            )


def test_recovery_has_no_sentinel_skip_root() -> None:
    """Item 9 follow-up: the `_SKIP_ROOT = object()` sentinel must stay removed."""
    module = _parse(_RECOVERY_MODULE_PATH)
    assert "_SKIP_ROOT" not in _assigned_names(module), (
        "`observability/_recovery.py` must not define a `_SKIP_ROOT` object sentinel. Use direct exception handling in the reconcile loop instead."
    )


def test_recovery_module_importable() -> None:
    """Ensure the parsed module also loads cleanly at runtime."""
    spec = importlib.util.spec_from_file_location("ai_pipeline_core.observability._recovery", _RECOVERY_MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
