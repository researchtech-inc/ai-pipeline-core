"""Stub-module helper for building non-exempt PromptContract / Methodology subclasses.

The integration tests under ``tests/integration/llm/`` are exempt from the
file-rules registry (their ``__module__`` contains the ``tests`` segment),
which short-circuits ``load_body_file`` to an empty body. To exercise the
template-driven path (``.md.j2`` / ``.md`` discovery + Jinja rendering),
this helper constructs classes whose ``__module__`` is a single-segment
stub module name (e.g. ``_pc_test_stub_pass_phrase``) registered in
``sys.modules`` with ``__file__`` pointing into ``contracts/``.

The stub module's ``__file__`` resolves to a path inside ``contracts/``;
``load_body_file`` calls ``_module_dir`` which returns the parent of
``__file__``, so the paired body files live alongside the stub-module
location. The ``.py`` path itself need not exist on disk — only the
parent directory matters.
"""

from __future__ import annotations

import re
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

from ai_pipeline_core import (
    FrozenBaseModel,
    Methodology,
    PromptContract,
    ToolAvailability,
    ValidationFailure,
)

__all__ = [
    "CONTRACTS_DIR",
    "make_stub_contract",
    "make_stub_methodology",
]

CONTRACTS_DIR = Path(__file__).resolve().parent / "contracts"

_SNAKE_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _snake(name: str) -> str:
    """PascalCase -> snake_case."""
    return _SNAKE_RE.sub("_", name).lower()


def _install_stub_module(module_name: str, body_dir: Path) -> None:
    """Register a fake module backed by ``body_dir/_stub_module.py`` in ``sys.modules``.

    The ``.py`` file need not exist; ``_module_dir`` only inspects the
    parent directory of ``__file__``.
    """
    existing = sys.modules.get(module_name)
    if existing is not None and getattr(existing, "__file__", None):
        # Already installed (reuse for repeated builds in the same session).
        return
    module = types.ModuleType(module_name)
    module.__file__ = str(body_dir / "_stub_module.py")
    sys.modules[module_name] = module


def make_stub_contract(
    *,
    name: str,
    output_type: type[FrozenBaseModel],
    purpose: str,
    returns: str,
    success_criteria: str,
    docstring: str | None = None,
    methodologies: tuple[type[Methodology], ...] = (),
    tools: tuple[ToolAvailability, ...] = (),
    annotations: dict[str, Any] | None = None,
    field_defaults: dict[str, Any] | None = None,
    validate_fn: Callable[[Any, Any], tuple[ValidationFailure, ...]] | None = None,
    body_dir: Path = CONTRACTS_DIR,
) -> type[PromptContract[Any]]:
    """Build a non-exempt ``PromptContract`` subclass whose paired body file lives in ``body_dir``.

    ``name`` is the contract's PascalCase class name ending in ``Contract``.
    The stub module is deterministically named ``_pc_test_stub_<snake(name)>``
    so re-importing the helper in the same session is a no-op for
    ``register_contract``.
    """
    if not name.endswith("Contract"):
        raise ValueError(f"Stub contract name must end with 'Contract': {name!r}")

    stem = _snake(name.removesuffix("Contract"))
    module_name = f"_pc_test_stub_{stem}"
    _install_stub_module(module_name, body_dir)

    namespace_annotations: dict[str, Any] = {} if annotations is None else dict(annotations)
    namespace_defaults: dict[str, Any] = {} if field_defaults is None else dict(field_defaults)

    def populate(namespace: dict[str, Any]) -> None:
        namespace["__module__"] = module_name
        namespace["__qualname__"] = name
        namespace["__doc__"] = docstring or f"Stub contract '{name}' for integration testing."
        namespace["__annotations__"] = {
            "purpose": ClassVar[str],
            "returns": ClassVar[str],
            "success_criteria": ClassVar[str],
            "methodologies": ClassVar[tuple[type[Methodology], ...]],
            "tools": ClassVar[tuple[ToolAvailability, ...]],
            **namespace_annotations,
        }
        namespace["purpose"] = purpose
        namespace["returns"] = returns
        namespace["success_criteria"] = success_criteria
        namespace["methodologies"] = methodologies
        namespace["tools"] = tools
        namespace.update(namespace_defaults)
        if validate_fn is not None:
            namespace["validate"] = validate_fn

    cls = types.new_class(name, (PromptContract[output_type],), {}, populate)
    return cls


def make_stub_methodology(
    *,
    name: str,
    purpose: str,
    docstring: str | None = None,
    extra_attrs: dict[str, Any] | None = None,
    body_dir: Path = CONTRACTS_DIR,
) -> type[Methodology]:
    """Build a non-exempt ``Methodology`` subclass whose paired body file lives in ``body_dir``.

    ``name`` is the methodology's PascalCase class name ending in ``Methodology``.
    """
    if not name.endswith("Methodology"):
        raise ValueError(f"Stub methodology name must end with 'Methodology': {name!r}")

    stem = _snake(name.removesuffix("Methodology"))
    module_name = f"_pc_test_stub_methodology_{stem}"
    _install_stub_module(module_name, body_dir)

    extras = extra_attrs or {}

    def populate(namespace: dict[str, Any]) -> None:
        namespace["__module__"] = module_name
        namespace["__qualname__"] = name
        namespace["__doc__"] = docstring or f"Stub methodology '{name}' for integration testing."
        namespace["__annotations__"] = {"purpose": ClassVar[str]}
        namespace["purpose"] = purpose
        namespace.update(extras)

    cls = types.new_class(name, (Methodology,), {}, populate)
    return cls
