"""Tests for current ModelOptions behavior."""

import pytest
from pydantic import ValidationError

from ai_pipeline_core.llm import ModelOptions
from ai_pipeline_core.llm._conversation_runtime import cache_overrides, generation_overrides, retry_overrides
from ai_pipeline_core.settings import Settings


class TestModelOptions:
    """Test public conversation option fields."""

    def test_default_values(self) -> None:
        options = ModelOptions()

        assert options.temperature is None
        assert options.system_prompt is None
        assert options.reasoning_effort is None
        assert options.retries is None
        assert options.retry_delay_seconds is None
        assert options.timeout is None
        assert options.cache_ttl is None
        assert options.max_completion_tokens is None
        assert options.stop is None
        assert options.verbosity is None
        assert options.min_output_tps is None

    def test_generation_overrides(self) -> None:
        options = ModelOptions(
            temperature=0.7,
            reasoning_effort="medium",
            verbosity="low",
            max_completion_tokens=20_000,
            stop=("STOP",),
        )

        spec = generation_overrides(options)

        assert spec is not None
        assert spec.temperature == pytest.approx(0.7)
        assert spec.reasoning_effort == "medium"
        assert spec.verbosity == "low"
        assert spec.max_completion_tokens == 20_000
        assert spec.stop == ("STOP",)

    def test_retry_overrides(self) -> None:
        options = ModelOptions(retries=5, retry_delay_seconds=15, timeout=120, min_output_tps=2.5)

        spec = retry_overrides(options)

        assert spec is not None
        assert spec.retries == 5
        assert spec.retry_delay_seconds == 15
        assert spec.timeout_s == 120
        assert spec.min_output_tps == pytest.approx(2.5)

    def test_cache_overrides_default_none(self) -> None:
        assert cache_overrides(ModelOptions()) is None

    def test_cache_overrides_with_ttl(self) -> None:
        spec = cache_overrides(ModelOptions(cache_ttl=10))

        assert spec is not None
        assert spec.ttl == 10

    def test_cache_overrides_explicit_zero_disables(self) -> None:
        spec = cache_overrides(ModelOptions(cache_ttl=0))

        assert spec is not None
        assert spec.ttl == 0

    def test_stop_single_string(self) -> None:
        options = ModelOptions(stop="STOP")
        assert options.stop == ("STOP",)

    def test_stop_list_of_strings(self) -> None:
        options = ModelOptions(stop=["STOP", "END", "\n\n"])
        assert options.stop == ("STOP", "END", "\n\n")

    def test_immutability(self) -> None:
        options = ModelOptions()
        with pytest.raises(ValidationError):
            options.timeout = 500  # type: ignore[misc]

    def test_model_copy_update(self) -> None:
        options = ModelOptions(timeout=300)
        updated = options.model_copy(update={"timeout": 500})

        assert updated.timeout == 500
        assert options.timeout == 300

    def test_positive_numeric_validation(self) -> None:
        with pytest.raises(ValidationError):
            ModelOptions(max_completion_tokens=0)
        with pytest.raises(ValidationError):
            ModelOptions(timeout=0)
        with pytest.raises(ValidationError):
            ModelOptions(min_output_tps=0)


class TestRetryDefaults:
    """Verify None vs explicit 0 semantics for retry fields."""

    def test_retries_none_by_default(self) -> None:
        options = ModelOptions()
        assert options.retries is None
        assert options.retry_delay_seconds is None

    def test_retries_explicit_zero(self) -> None:
        options = ModelOptions(retries=0, retry_delay_seconds=0)
        assert options.retries == 0
        assert options.retry_delay_seconds == 0

    def test_retries_explicit_value(self) -> None:
        options = ModelOptions(retries=5, retry_delay_seconds=10)
        assert options.retries == 5
        assert options.retry_delay_seconds == 10

    def test_settings_conversation_retries_defaults(self) -> None:
        settings = Settings()

        assert settings.conversation_retries == 2
        assert settings.conversation_retry_delay_seconds == 30
        assert settings.conversation_retry_backoff_multiplier == 3
        assert settings.conversation_retry_max_delay_seconds == 300
