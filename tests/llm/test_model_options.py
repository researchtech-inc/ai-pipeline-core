"""Tests for ModelOptions."""

import pytest
from pydantic import ValidationError

from ai_pipeline_core.llm import ModelOptions


class TestModelOptions:
    """Test ModelOptions configuration."""

    def test_default_values(self):
        """Test default ModelOptions values."""
        options = ModelOptions()
        assert options.system_prompt is None
        assert options.search_context_size is None
        assert options.reasoning_effort is None
        assert options.retries is None
        assert options.retry_delay_seconds is None
        assert options.timeout == 600
        assert options.cache_ttl == "300s"
        assert options.max_completion_tokens is None
        assert options.stop is None
        assert options.verbosity is None
        assert options.usage_tracking is True
        assert options.user is None
        assert options.metadata is None
        assert options.extra_body is None
        assert options.cache_warmup_max_wait is None
        assert options.cache_warmup_max_qps is None

    def test_to_openai_kwargs_defaults(self):
        """Test conversion to OpenAI kwargs with defaults."""
        options = ModelOptions()
        kwargs = options.to_openai_completion_kwargs()

        assert kwargs["timeout"] == 600
        assert "extra_body" in kwargs
        assert kwargs["extra_body"] == {"usage": {"include": True}}

        # These should not be in kwargs when None
        assert "max_completion_tokens" not in kwargs
        assert "response_format" not in kwargs
        assert "verbosity" not in kwargs

    def test_max_completion_tokens(self):
        """Test max_completion_tokens pass-through."""
        options = ModelOptions(max_completion_tokens=10000)
        kwargs = options.to_openai_completion_kwargs()
        assert kwargs["max_completion_tokens"] == 10000

    def test_search_context_size(self):
        """Test search_context_size values."""
        for size in ["low", "medium", "high"]:
            options = ModelOptions(search_context_size=size)  # type: ignore
            kwargs = options.to_openai_completion_kwargs()
            assert kwargs["web_search_options"]["search_context_size"] == size

    def test_all_options_combined(self):
        """Test all options combined."""
        options = ModelOptions(
            system_prompt="You are a helpful assistant",
            search_context_size="high",
            reasoning_effort="medium",
            retries=5,
            retry_delay_seconds=15,
            timeout=600,
            max_completion_tokens=20000,
        )

        # Test with non-grok model
        kwargs = options.to_openai_completion_kwargs()

        assert kwargs["timeout"] == 600
        assert kwargs["web_search_options"]["search_context_size"] == "high"
        assert kwargs["reasoning_effort"] == "medium"
        assert kwargs["max_completion_tokens"] == 20000
        assert "extra_body" in kwargs

        # System prompt is not in kwargs (handled separately)
        assert "system_prompt" not in kwargs
        # Retry settings are not in OpenAI kwargs
        assert "retries" not in kwargs
        assert "retry_delay_seconds" not in kwargs

    def test_reasoning_effort_always_included_when_set(self):
        """Test that reasoning_effort is included when set, regardless of model check."""
        options = ModelOptions(reasoning_effort="low")

        # Without model parameter
        kwargs = options.to_openai_completion_kwargs()
        assert kwargs["reasoning_effort"] == "low"

        # With non-grok model
        kwargs = options.to_openai_completion_kwargs()
        assert kwargs["reasoning_effort"] == "low"

    def test_immutability(self):
        """Test that ModelOptions is frozen (immutable)."""
        options = ModelOptions()
        with pytest.raises(ValidationError):
            options.timeout = 500  # type: ignore[misc]

    def test_model_copy_update(self):
        """Test that frozen ModelOptions can be updated via model_copy."""
        options = ModelOptions(timeout=300)
        updated = options.model_copy(update={"timeout": 500})
        assert updated.timeout == 500
        assert options.timeout == 300  # original unchanged

    def test_verbosity_option(self):
        """Test verbosity option."""
        for level in ["low", "medium", "high"]:
            options = ModelOptions(verbosity=level)  # type: ignore
            kwargs = options.to_openai_completion_kwargs()
            assert kwargs["verbosity"] == level

    def test_usage_tracking_disabled(self):
        """Test usage tracking can be disabled."""
        options = ModelOptions(usage_tracking=False)
        kwargs = options.to_openai_completion_kwargs()
        assert "usage" not in kwargs["extra_body"]

    def test_usage_tracking_enabled(self):
        """Test usage tracking is enabled by default."""
        options = ModelOptions(usage_tracking=True)
        kwargs = options.to_openai_completion_kwargs()
        assert kwargs["extra_body"]["usage"] == {"include": True}

    def test_stop_single_string(self):
        """Test stop parameter with single string is normalized to tuple."""
        options = ModelOptions(stop="STOP")
        assert options.stop == ("STOP",)
        kwargs = options.to_openai_completion_kwargs()
        assert kwargs["stop"] == ["STOP"]

    def test_stop_list_of_strings(self):
        """Test stop parameter with list of strings is normalized to tuple."""
        options = ModelOptions(stop=["STOP", "END", "\n\n"])
        assert options.stop == ("STOP", "END", "\n\n")
        kwargs = options.to_openai_completion_kwargs()
        assert kwargs["stop"] == ["STOP", "END", "\n\n"]

    def test_stop_none_not_included(self):
        """Test that stop is not included when None."""
        options = ModelOptions(stop=None)
        kwargs = options.to_openai_completion_kwargs()
        assert "stop" not in kwargs

    def test_extra_body_custom_parameters(self):
        """Test extra_body with custom parameters."""
        custom_params = {
            "custom_field": "value",
            "another_param": 123,
            "nested": {"key": "value"},
        }
        options = ModelOptions(extra_body=custom_params, usage_tracking=False)
        kwargs = options.to_openai_completion_kwargs()
        assert kwargs["extra_body"] == custom_params

    def test_extra_body_none_defaults_to_empty_dict(self):
        """Test that extra_body defaults to empty dict when None."""
        options = ModelOptions(extra_body=None, usage_tracking=False)
        kwargs = options.to_openai_completion_kwargs()
        assert kwargs["extra_body"] == {}

    def test_extra_body_with_usage_tracking(self):
        """Test extra_body is replaced when usage_tracking is enabled."""
        custom_params = {"custom_field": "value"}
        options = ModelOptions(extra_body=custom_params, usage_tracking=True)
        kwargs = options.to_openai_completion_kwargs()
        # extra_body is replaced with custom_params, then usage is added
        assert kwargs["extra_body"]["custom_field"] == "value"
        assert kwargs["extra_body"]["usage"] == {"include": True}

    def test_stop_with_multiple_parameters(self):
        """Test stop combined with other parameters."""
        options = ModelOptions(
            stop=["STOP", "END"],
            temperature=0.7,
            max_completion_tokens=1000,
        )
        kwargs = options.to_openai_completion_kwargs()
        assert kwargs["stop"] == ["STOP", "END"]
        assert kwargs["temperature"] == pytest.approx(0.7)
        assert kwargs["max_completion_tokens"] == 1000

    def test_extra_body_preserves_custom_fields_with_usage_tracking(self):
        """Test that custom extra_body fields are preserved when usage_tracking adds its field."""
        options = ModelOptions(extra_body={"custom": "data", "another": 42}, usage_tracking=True)
        kwargs = options.to_openai_completion_kwargs()

        # Custom fields should be present
        assert kwargs["extra_body"]["custom"] == "data"
        assert kwargs["extra_body"]["another"] == 42
        # Usage tracking should also be present
        assert kwargs["extra_body"]["usage"] == {"include": True}

    def test_user_identifier(self):
        """Test user parameter for cost tracking."""
        options = ModelOptions(user="user_12345")
        kwargs = options.to_openai_completion_kwargs()
        assert kwargs["user"] == "user_12345"

    def test_user_none_not_included(self):
        """Test that user is not included when None."""
        options = ModelOptions(user=None)
        kwargs = options.to_openai_completion_kwargs()
        assert "user" not in kwargs

    def test_user_with_other_parameters(self):
        """Test user combined with other parameters."""
        options = ModelOptions(
            user="user_abc123",
            temperature=0.7,
            max_completion_tokens=1000,
            usage_tracking=True,
        )
        kwargs = options.to_openai_completion_kwargs()
        assert kwargs["user"] == "user_abc123"
        assert kwargs["temperature"] == pytest.approx(0.7)
        assert kwargs["max_completion_tokens"] == 1000
        assert kwargs["extra_body"]["usage"] == {"include": True}

    def test_user_long_identifier(self):
        """Test user with longer identifier string."""
        long_user_id = "user_" + "a" * 250  # 256 chars total
        options = ModelOptions(user=long_user_id)
        kwargs = options.to_openai_completion_kwargs()
        assert kwargs["user"] == long_user_id


class TestRetryDefaults:
    """Verify None vs explicit 0 semantics for retries/retry_delay_seconds."""

    def test_retries_none_by_default(self):
        options = ModelOptions()
        assert options.retries is None
        assert options.retry_delay_seconds is None

    def test_retries_explicit_zero(self):
        options = ModelOptions(retries=0, retry_delay_seconds=0)
        assert options.retries == 0
        assert options.retry_delay_seconds == 0

    def test_retries_explicit_value(self):
        options = ModelOptions(retries=5, retry_delay_seconds=10)
        assert options.retries == 5
        assert options.retry_delay_seconds == 10

    def test_retries_not_in_openai_kwargs(self):
        options = ModelOptions(retries=3, retry_delay_seconds=15)
        kwargs = options.to_openai_completion_kwargs()
        assert "retries" not in kwargs
        assert "retry_delay_seconds" not in kwargs

    def test_settings_conversation_retries_defaults(self):
        from ai_pipeline_core.settings import Settings

        s = Settings()
        assert s.conversation_retries == 3
        assert s.conversation_retry_delay_seconds == 30
        assert s.conversation_retry_backoff_multiplier == 3
        assert s.conversation_retry_max_delay_seconds == 300
