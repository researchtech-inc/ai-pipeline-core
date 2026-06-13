"""Unit tests for HTTP pool sizing and Retry-After-aware retry backoff."""

import pytest

from ai_pipeline_core._llm_core import _transport
from ai_pipeline_core._llm_core import client as llm_client
from ai_pipeline_core._llm_core._aipl_headers import AIPLResponseHeaders
from ai_pipeline_core._llm_core._config import LLMCoreConfig, override_config


class TestHttpPoolConfig:
    """The httpx connection pool is driven by config, not hardcoded constants."""

    def test_limits_built_from_config(self) -> None:
        config = LLMCoreConfig(
            http_max_connections=512,
            http_max_keepalive_connections=64,
            http_keepalive_expiry_s=45.0,
        )
        limits = _transport._build_http_limits(config)
        assert limits.max_connections == 512
        assert limits.max_keepalive_connections == 64
        assert limits.keepalive_expiry == 45.0

    def test_defaults_match_distributed_posture(self) -> None:
        config = LLMCoreConfig()
        assert config.http_max_connections == 200
        assert config.http_max_keepalive_connections == 100
        # Aligned to the proxy keepalive (60s), not the legacy 300s.
        assert config.http_keepalive_expiry_s == 60.0


class TestRetryAfterParsing:
    """``Retry-After`` is parsed in delta-seconds form and never raises on a date."""

    def test_delta_seconds_parsed(self) -> None:
        headers = AIPLResponseHeaders.from_response_headers({"retry-after": "12"})
        assert headers.retry_after_s == 12.0

    def test_http_date_form_ignored(self) -> None:
        headers = AIPLResponseHeaders.from_response_headers({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"})
        assert headers.retry_after_s is None

    def test_absent_header(self) -> None:
        headers = AIPLResponseHeaders.from_response_headers({"x-aipl-call-id": "abc"})
        assert headers.retry_after_s is None


class TestRetryDelay:
    """``_compute_retry_delay`` honors ``Retry-After`` as a floor, bounded by the max."""

    @pytest.fixture
    def config(self) -> LLMCoreConfig:
        return LLMCoreConfig(
            conversation_retry_delay_seconds=30.0,
            conversation_retry_backoff_multiplier=3.0,
            conversation_retry_max_delay_seconds=300.0,
        )

    def test_exponential_backoff_without_retry_after(self, config: LLMCoreConfig) -> None:
        with override_config(config):
            assert llm_client._compute_retry_delay(0, None, None) == 30.0
            assert llm_client._compute_retry_delay(1, None, None) == 90.0

    def test_retry_after_acts_as_floor(self, config: LLMCoreConfig) -> None:
        with override_config(config):
            # Larger than the attempt-0 backoff (30s) -> honored.
            assert llm_client._compute_retry_delay(0, None, 75.0) == 75.0
            # Smaller than the attempt-1 backoff (90s) -> backoff wins.
            assert llm_client._compute_retry_delay(1, None, 10.0) == 90.0

    def test_retry_after_capped_at_max(self, config: LLMCoreConfig) -> None:
        with override_config(config):
            assert llm_client._compute_retry_delay(0, None, 9999.0) == 300.0

    def test_fixed_delay_with_retry_after_floor(self, config: LLMCoreConfig) -> None:
        with override_config(config):
            # Caller fixed 10s, server says 50s -> server floor wins.
            assert llm_client._compute_retry_delay(0, 10.0, 50.0) == 50.0
            # Caller fixed 40s, no Retry-After -> fixed used.
            assert llm_client._compute_retry_delay(0, 40.0, None) == 40.0

    def test_retry_after_floor_survives_jitter(self, config: LLMCoreConfig) -> None:
        """Regression: a downward jitter must never drop the sleep below the server cool-off."""
        with override_config(config):
            # Worst-case downward jitter (0.8) on a 30s backoff would be 24s; the 50s
            # Retry-After floor, enforced AFTER jitter, must still hold.
            assert llm_client._compute_retry_delay(0, None, 50.0, jitter=0.8) == 50.0
            assert llm_client._compute_retry_delay(0, None, 50.0, jitter=1.2) == 50.0
            # When the backoff dominates the floor, jitter applies to the backoff.
            assert llm_client._compute_retry_delay(1, None, 10.0, jitter=0.8) == pytest.approx(72.0)
            # An absurd Retry-After stays bounded by the max delay even at min jitter (anti-DoS).
            assert llm_client._compute_retry_delay(0, None, 9999.0, jitter=0.8) == 300.0


class TestConfigValidation:
    """The HTTP pool knobs are validated at config load, not at client construction."""

    def test_rejects_nonpositive_max_connections(self) -> None:
        with pytest.raises(ValueError, match="http_max_connections"):
            LLMCoreConfig(http_max_connections=0)

    def test_rejects_negative_keepalive_connections(self) -> None:
        with pytest.raises(ValueError, match="http_max_keepalive_connections"):
            LLMCoreConfig(http_max_keepalive_connections=-1)

    def test_rejects_nonpositive_keepalive_expiry(self) -> None:
        with pytest.raises(ValueError, match="http_keepalive_expiry_s"):
            LLMCoreConfig(http_keepalive_expiry_s=0.0)

    def test_accepts_valid_overrides(self) -> None:
        config = LLMCoreConfig(
            http_max_connections=300,
            http_max_keepalive_connections=0,
            http_keepalive_expiry_s=30.0,
        )
        assert config.http_max_connections == 300
