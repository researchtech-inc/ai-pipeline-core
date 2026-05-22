"""Test to demonstrate type-safe FILES enum usage."""

from enum import StrEnum

import pytest

from ai_pipeline_core.documents import Document


class AllowedInputFiles(StrEnum):
    """Demonstrate FILES enum for document validation."""

    CONFIG = "config.yaml"
    DATA = "data.json"
    TEXT = "input.txt"


class InputDocument(Document):
    """Flow document with filename restrictions."""

    # Proper type annotation for type safety
    # Use the same base type but assign the specific enum
    FILES = AllowedInputFiles

    def get_type(self) -> str:
        return "input"


@pytest.fixture(autouse=True)
def _suppress_registration():
    return


def test_files_enum_type_safety():
    """Test that FILES enum provides type safety."""
    # This works and is type-safe - pyright knows the type
    assert InputDocument.FILES.CONFIG == "config.yaml"
    assert InputDocument.FILES.DATA == "data.json"
    assert InputDocument.FILES.TEXT == "input.txt"

    # Can also iterate over values
    values = list(InputDocument.FILES)
    assert len(values) == 3
    assert AllowedInputFiles.CONFIG in values

    # Create document with valid name
    doc = InputDocument(
        name=InputDocument.FILES.CONFIG,  # Type-safe access
        content=b"test: value",
        description="Config file",
    )
    assert doc.name == "config.yaml"

    # Alternative way using the enum directly
    doc2 = InputDocument(
        name=AllowedInputFiles.DATA,
        content=b'{"key": "value"}',
        description="Data file",
    )
    assert doc2.name == "data.json"


def test_files_enum_validation():
    """Test that FILES validation still works."""
    from ai_pipeline_core.exceptions import DocumentNameError

    # Valid names should work
    doc = InputDocument(
        name="config.yaml",
        content=b"test",
        description=None,
    )
    assert doc.name == "config.yaml"

    # Invalid names should fail
    with pytest.raises(DocumentNameError) as exc_info:
        InputDocument(
            name="invalid.txt",
            content=b"test",
            description=None,
        )
    assert "Invalid filename" in str(exc_info.value)
    assert "Allowed:" in str(exc_info.value)
