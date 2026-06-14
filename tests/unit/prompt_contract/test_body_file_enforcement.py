"""Enforcement of paired body file at PromptContract / Methodology subclass creation.

The body-file loader raises ``TypeError`` for non-exempt classes that ship no
paired ``.j2`` template. These tests exercise that contract through the public
subclass-creation path (``__init_subclass__``) rather than calling
``load_body_file`` directly, so the enforcement is pinned where application code
actually meets the framework.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any, ClassVar

import pytest

from ai_pipeline_core import FrozenBaseModel
from ai_pipeline_core.prompt_contract import Methodology, PromptContract


class _BodyOutput(FrozenBaseModel):
    """Trivial output type."""

    answer: str


def _install_nonexempt_module(monkeypatch: pytest.MonkeyPatch, name: str, file: Path) -> str:
    """Register a stub module whose name is non-exempt (single segment, no 'tests' / 'test_')."""
    module_name = f"_pc_enforcement_stub_{name}"
    module = types.ModuleType(module_name)
    module.__file__ = str(file)
    monkeypatch.setitem(sys.modules, module_name, module)
    return module_name


def _build_contract(module_name: str, class_name: str) -> type[PromptContract[_BodyOutput]]:
    def populate(ns: dict[str, Any]) -> None:
        ns["__module__"] = module_name
        ns["__qualname__"] = class_name
        ns["__doc__"] = "Doc."
        ns["__annotations__"] = {
            "purpose": ClassVar[str],
            "returns": ClassVar[str],
            "success_criteria": ClassVar[str],
        }
        ns["purpose"] = "p"
        ns["returns"] = "r"
        ns["success_criteria"] = "s"

    return types.new_class(class_name, (PromptContract[_BodyOutput],), {}, populate)


def _build_methodology(module_name: str, class_name: str) -> type[Methodology]:
    def populate(ns: dict[str, Any]) -> None:
        ns["__module__"] = module_name
        ns["__qualname__"] = class_name
        ns["__doc__"] = "Doc."
        ns["__annotations__"] = {"purpose": ClassVar[str]}
        ns["purpose"] = "guide"

    return types.new_class(class_name, (Methodology,), {}, populate)


# ---------------------------------------------------------------------------
# PromptContract
# ---------------------------------------------------------------------------


class TestPromptContractBodyFileEnforcement:
    """Non-exempt contracts must have a paired ``.j2`` body file; exempt ones may omit it."""

    def test_non_exempt_contract_without_body_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Class creation fails when no ``.j2`` exists for a non-exempt module."""
        py_file = tmp_path / "stub.py"
        py_file.write_text("# stub")
        module_name = _install_nonexempt_module(monkeypatch, "missing_body", py_file)

        with pytest.raises(TypeError, match="expects a paired Jinja body file"):
            _build_contract(module_name, "MissingBodyContract")

    def test_non_exempt_contract_with_jinja_body_succeeds(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A paired ``.j2`` template satisfies the enforcement."""
        py_file = tmp_path / "stub.py"
        py_file.write_text("# stub")
        (tmp_path / "with_jinja_body.j2").write_text("## Header\n\nhello {{ contract.purpose }}\n")
        module_name = _install_nonexempt_module(monkeypatch, "with_jinja_body", py_file)

        cls = _build_contract(module_name, "WithJinjaBodyContract")
        assert "{{ contract.purpose }}" in cls._body

    def test_exempt_contract_without_body_succeeds(self) -> None:
        """A unit-test contract (exempt by module path) needs no body file."""

        class ExemptNoBodyContract(PromptContract[_BodyOutput]):
            """Exempt contract — defined in tests.unit.* (auto-exempt)."""

            purpose: ClassVar[str] = "p"
            returns: ClassVar[str] = "r"
            success_criteria: ClassVar[str] = "s"

        assert ExemptNoBodyContract._body == ""


# ---------------------------------------------------------------------------
# Methodology
# ---------------------------------------------------------------------------


class TestMethodologyBodyFileEnforcement:
    """Non-exempt methodologies must have a paired ``.j2`` body file; exempt ones may omit it."""

    def test_non_exempt_methodology_without_body_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Class creation fails when no ``.j2`` exists for a non-exempt methodology."""
        py_file = tmp_path / "stub.py"
        py_file.write_text("# stub")
        module_name = _install_nonexempt_module(monkeypatch, "missing_meth_body", py_file)

        with pytest.raises(TypeError, match="expects a paired Jinja body file"):
            _build_methodology(module_name, "MissingBodyMethodology")

    def test_non_exempt_methodology_with_jinja_body_succeeds(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A paired ``.j2`` template satisfies the methodology enforcement."""
        py_file = tmp_path / "stub.py"
        py_file.write_text("# stub")
        (tmp_path / "with_jinja_body.j2").write_text("## Rubric\n\n{{ methodology.purpose }}\n")
        module_name = _install_nonexempt_module(monkeypatch, "with_jinja_meth_body", py_file)

        cls = _build_methodology(module_name, "WithJinjaBodyMethodology")
        assert "{{ methodology.purpose }}" in cls._body

    def test_exempt_methodology_without_body_succeeds(self) -> None:
        """A unit-test methodology (exempt by module path) needs no body file."""

        class ExemptNoBodyMethodology(Methodology):
            """Exempt methodology — defined in tests.unit.* (auto-exempt)."""

            purpose: ClassVar[str] = "guide"

        assert ExemptNoBodyMethodology._body == ""
