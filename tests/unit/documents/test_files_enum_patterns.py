"""Demonstrate type-safe FILES enum usage patterns."""

from enum import StrEnum
from typing import ClassVar, cast

import pytest

from ai_pipeline_core.documents import Document
from ai_pipeline_core.exceptions import DocumentNameError


class AllowedInputFiles(StrEnum):
    """Demonstrate FILES enum for document validation."""

    CONFIG = "config.yaml"
    DATA = "data.json"
    TEXT = "input.txt"


@pytest.fixture(autouse=True)
def _suppress_registration():
    return


# Solution 1: Use the enum directly (simplest and type-safe)
class InputDocumentSimple(Document):
    """Flow document using enum directly."""

    FILES = AllowedInputFiles

    def get_type(self) -> str:
        return "input"


def test_simple_approach():
    """Test using enum directly without ClassVar."""
    # Access via the enum class - fully type safe
    assert AllowedInputFiles.CONFIG == "config.yaml"
    assert AllowedInputFiles.DATA == "data.json"

    # Create document using enum values
    doc = InputDocumentSimple(
        name=AllowedInputFiles.CONFIG,  # Type-safe
        content=b"test: value",
        description="Config file",
    )
    assert doc.name == "config.yaml"


# Solution 2: Use cast for type narrowing when needed
class InputDocumentCast(Document):
    """Flow document with type casting for FILES access."""

    FILES: ClassVar[type[StrEnum] | None] = AllowedInputFiles

    @classmethod
    def get_files_enum(cls) -> type[AllowedInputFiles]:
        """Type-safe accessor for FILES enum."""
        return cast(type[AllowedInputFiles], cls.FILES)

    def get_type(self) -> str:
        return "input"


def test_cast_approach():
    """Test using cast for type safety."""
    # Use the type-safe accessor
    files = InputDocumentCast.get_files_enum()
    assert files.CONFIG == "config.yaml"
    assert files.DATA == "data.json"

    # Or use the enum directly
    assert AllowedInputFiles.CONFIG == "config.yaml"

    doc = InputDocumentCast(
        name=AllowedInputFiles.CONFIG,
        content=b"test: value",
        description="Config file",
    )
    assert doc.name == "config.yaml"


# Solution 3: Document pattern - just use the enum directly
class InputDocumentPattern(Document):
    """Best practice: use enum directly, document the pattern."""

    # For validation, set FILES to the enum class
    FILES = AllowedInputFiles

    # Document the allowed files for users
    # Users should use AllowedInputFiles.CONFIG, etc. directly

    def get_type(self) -> str:
        return "input"


def test_pattern_approach():
    """Test the recommended pattern."""
    # Always use the enum directly - cleanest and type-safe
    config_name = AllowedInputFiles.CONFIG
    data_name = AllowedInputFiles.DATA

    assert config_name == "config.yaml"
    assert data_name == "data.json"

    # Create documents using enum values
    doc = InputDocumentPattern(
        name=AllowedInputFiles.CONFIG,  # Clear and type-safe
        content=b"test: value",
        description="Config file",
    )
    assert doc.name == "config.yaml"

    # Validation still works
    with pytest.raises(DocumentNameError):
        InputDocumentPattern(
            name="invalid.txt",
            content=b"test",
            description=None,
        )
