"""Regression tests for removed cache-warmup ModelOptions fields."""

import pytest
from pydantic import ValidationError

from ai_pipeline_core.llm import ModelOptions


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("cache_warmup_max_wait", 300.0),
        ("cache_warmup_max_qps", 15),
    ],
)
def test_cache_warmup_options_are_not_public_model_options(field_name: str, value: object) -> None:
    """Cache warmup is no longer configured through Conversation ModelOptions."""
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ModelOptions(**{field_name: value})
