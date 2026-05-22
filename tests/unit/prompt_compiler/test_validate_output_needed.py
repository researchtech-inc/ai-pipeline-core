"""Prove PromptSpec has no post-parse semantic validation hook.

After Pydantic validates types, there is no hook for semantic validation
(index bounds, date formats, reference existence).
"""

from datetime import date

import pytest
from pydantic import BaseModel

from ai_pipeline_core.prompt_compiler.spec import PromptSpec


class TestNoValidateOutputHook:
    """Prove PromptSpec has no validate_output method."""

    def test_prompt_spec_has_no_validate_output(self) -> None:
        """PromptSpec has no validate_output classmethod — passes on current code."""
        assert not hasattr(PromptSpec, "validate_output")

    def test_pydantic_accepts_semantically_invalid_output(self) -> None:
        """Pydantic validation passes for type-correct but semantically wrong data."""

        class AnalysisOutput(BaseModel):
            assertion_indices: tuple[int, ...]
            event_date: str
            provider_id: str

        result = AnalysisOutput(
            assertion_indices=(0, 5, 99),
            event_date="mid-2023",
            provider_id="NO_QUALIFIED_PROVIDER",
        )
        assert result.assertion_indices == (0, 5, 99)

        with pytest.raises(ValueError):
            date.fromisoformat(result.event_date)

        valid_assertions = ["A-001", "A-002", "A-003"]
        with pytest.raises(IndexError):
            _ = valid_assertions[5]


class TestValidateOutputNotImplemented:
    """Verify validate_output() after implementation."""

    @pytest.mark.xfail(reason="validate_output() hook not yet implemented", strict=True)
    def test_validate_output_method_exists(self) -> None:
        """After fix: PromptSpec has validate_output classmethod."""
        assert hasattr(PromptSpec, "validate_output")
        assert callable(PromptSpec.validate_output)

    @pytest.mark.xfail(reason="OutputValidationError not yet implemented", strict=True)
    def test_output_validation_error_importable(self) -> None:
        """After fix: OutputValidationError is importable."""
        from ai_pipeline_core.prompt_compiler._validation import OutputValidationError  # type: ignore[import-not-found]

        assert OutputValidationError is not None
