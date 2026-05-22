"""Tests for ValidationFailure."""

import pytest

from ai_pipeline_core.prompt_contract import ValidationFailure


def test_validation_failure_minimal() -> None:
    failure = ValidationFailure(message="something failed")
    assert failure.message == "something failed"
    assert failure.field is None


def test_validation_failure_with_field_path() -> None:
    failure = ValidationFailure(field="items[0].score", message="score out of range")
    assert failure.field == "items[0].score"
    assert failure.message == "score out of range"


def test_validation_failure_is_frozen() -> None:
    from pydantic import ValidationError

    failure = ValidationFailure(message="x")
    with pytest.raises(ValidationError):
        failure.message = "y"  # type: ignore[misc]  # frozen model mutation negative test
