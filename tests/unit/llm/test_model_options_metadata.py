"""Regression tests for removed provider-kwargs ModelOptions fields."""

import pytest
from pydantic import ValidationError

from ai_pipeline_core.llm import ModelOptions


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("metadata", {"experiment": "v1"}),
        ("extra_body", {"custom": "value"}),
        ("user", "user_123"),
        ("usage_tracking", True),
    ],
)
def test_provider_kwargs_are_not_model_options(field_name: str, value: object) -> None:
    """ModelOptions only carries framework-level conversation overrides."""
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ModelOptions(**{field_name: value})
