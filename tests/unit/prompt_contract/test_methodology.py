"""Tests for prompt_contract._methodology (Methodology base class)."""

from typing import ClassVar

import pytest

from ai_pipeline_core.prompt_contract import Methodology


def test_methodology_subclass_accepted() -> None:
    class GoodMethodology(Methodology):
        """A good methodology."""

        purpose: ClassVar[str] = "use these steps"

    assert GoodMethodology.purpose == "use these steps"


def test_methodology_subclass_name_must_end_with_methodology() -> None:
    with pytest.raises(TypeError, match="name must end with 'Methodology'"):

        class BadName(Methodology):
            """Bad name."""

            purpose: ClassVar[str] = "x"


def test_methodology_requires_docstring() -> None:
    with pytest.raises(TypeError, match="must define a non-empty docstring"):

        class MissingDocMethodology(Methodology):
            purpose: ClassVar[str] = "x"


def test_methodology_rejects_whitespace_docstring() -> None:
    with pytest.raises(TypeError, match="must define a non-empty docstring"):

        class WhitespaceMethodology(Methodology):
            """ """

            purpose: ClassVar[str] = "x"


def test_methodology_requires_purpose() -> None:
    with pytest.raises(TypeError, match="must define 'purpose'"):

        class NoPurposeMethodology(Methodology):
            """Doc."""


def test_methodology_rejects_empty_purpose() -> None:
    with pytest.raises(TypeError, match="purpose must not be empty"):

        class EmptyPurposeMethodology(Methodology):
            """Doc."""

            purpose: ClassVar[str] = "   "


def test_methodology_rejects_non_string_purpose() -> None:
    with pytest.raises(TypeError, match="purpose must be a string"):

        class BadTypePurposeMethodology(Methodology):
            """Doc."""

            purpose = 42  # type: ignore[assignment]


def test_methodology_cannot_be_instantiated() -> None:
    class FrameworkMethodology(Methodology):
        """Framework methodology."""

        purpose: ClassVar[str] = "x"

    with pytest.raises(TypeError, match="class-only and cannot be instantiated"):
        FrameworkMethodology()


def test_methodology_base_cannot_be_instantiated_either() -> None:
    """Even the base Methodology class refuses construction."""
    with pytest.raises(TypeError, match="class-only and cannot be instantiated"):
        Methodology()
