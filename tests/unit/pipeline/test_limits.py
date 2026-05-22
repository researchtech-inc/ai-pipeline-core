"""Tests for pipeline concurrency limits — LimitKind, PipelineLimit, pipeline_concurrency(), validation, upsert."""

import asyncio
from collections.abc import Mapping
from types import MappingProxyType
from unittest.mock import AsyncMock, patch

import pytest
from prefect.concurrency.asyncio import AcquireConcurrencySlotTimeoutError, ConcurrencySlotAcquisitionError

from ai_pipeline_core.pipeline.limits import (
    LimitKind,
    PipelineLimit,
    _LimitsState,
    _SharedStatus,
    _ensure_concurrency_limits,
    _limits_state,
    _set_limits_state,
    _slot_decay_per_second,
    _validate_concurrency_limits,
    pipeline_concurrency,
)


# ---------------------------------------------------------------------------
# LimitKind
# ---------------------------------------------------------------------------


class TestLimitKind:
    def test_values(self):
        assert LimitKind.CONCURRENT == "concurrent"
        assert LimitKind.PER_MINUTE == "per_minute"
        assert LimitKind.PER_HOUR == "per_hour"

    def test_is_str_enum(self):
        assert isinstance(LimitKind.CONCURRENT, str)


# ---------------------------------------------------------------------------
# PipelineLimit
# ---------------------------------------------------------------------------


class TestPipelineLimit:
    def test_defaults(self):
        pl = PipelineLimit(limit=100)
        assert pl.limit == 100
        assert pl.kind == LimitKind.CONCURRENT
        assert pl.timeout == 600

    def test_custom_values(self):
        pl = PipelineLimit(limit=15, kind=LimitKind.PER_MINUTE, timeout=300)
        assert pl.limit == 15
        assert pl.kind == LimitKind.PER_MINUTE
        assert pl.timeout == 300

    def test_frozen(self):
        pl = PipelineLimit(limit=10)
        with pytest.raises(AttributeError):
            pl.limit = 20  # type: ignore[misc]

    def test_limit_must_be_positive(self):
        with pytest.raises(ValueError, match="limit must be >= 1"):
            PipelineLimit(limit=0)
        with pytest.raises(ValueError, match="limit must be >= 1"):
            PipelineLimit(limit=-5)

    def test_timeout_must_be_positive(self):
        with pytest.raises(ValueError, match="timeout must be > 0"):
            PipelineLimit(limit=10, timeout=0)
        with pytest.raises(ValueError, match="timeout must be > 0"):
            PipelineLimit(limit=10, timeout=-1)


# ---------------------------------------------------------------------------
# _SharedStatus
# ---------------------------------------------------------------------------


class TestSharedStatus:
    def test_initial_state(self):
        status = _SharedStatus()
        assert status.prefect_available is True


# ---------------------------------------------------------------------------
# _slot_decay_per_second
# ---------------------------------------------------------------------------


class TestSlotDecay:
    def test_concurrent_returns_zero(self):
        assert _slot_decay_per_second(PipelineLimit(100, LimitKind.CONCURRENT)) == pytest.approx(0.0)

    def test_per_minute(self):
        result = _slot_decay_per_second(PipelineLimit(60, LimitKind.PER_MINUTE))
        assert result == pytest.approx(1.0)  # 60/60

    def test_per_hour(self):
        result = _slot_decay_per_second(PipelineLimit(3600, LimitKind.PER_HOUR))
        assert result == pytest.approx(1.0)  # 3600/3600

    def test_per_minute_fractional(self):
        result = _slot_decay_per_second(PipelineLimit(15, LimitKind.PER_MINUTE))
        assert result == pytest.approx(0.25)  # 15/60


# ---------------------------------------------------------------------------
# _validate_concurrency_limits
# ---------------------------------------------------------------------------


class TestValidateConcurrencyLimits:
    def test_empty_returns_empty(self):
        result = _validate_concurrency_limits("TestDeploy", {})
        assert isinstance(result, MappingProxyType)
        assert len(result) == 0

    def test_valid_limits(self):
        raw = {
            "brightdata": PipelineLimit(500),
            "scrapfly": PipelineLimit(15, LimitKind.PER_MINUTE),
        }
        result = _validate_concurrency_limits("TestDeploy", raw)
        assert isinstance(result, MappingProxyType)
        assert len(result) == 2
        assert result["brightdata"].limit == 500

    def test_invalid_name_not_str(self):
        with pytest.raises(TypeError, match="key must be str"):
            _validate_concurrency_limits("TestDeploy", {123: PipelineLimit(10)})  # type: ignore[dict-item]

    @pytest.mark.ai_docs
    def test_invalid_name_pattern(self):
        with pytest.raises(TypeError, match="invalid name"):
            _validate_concurrency_limits("TestDeploy", {"bad name!": PipelineLimit(10)})

    def test_invalid_config_type(self):
        with pytest.raises(TypeError, match="must be PipelineLimit"):
            _validate_concurrency_limits("TestDeploy", {"test": "not a limit"})  # type: ignore[dict-item]

    def test_invalid_kind_type(self):
        """Test that kind must be LimitKind enum instance."""
        # Create a PipelineLimit-like object with wrong kind type
        limit = PipelineLimit.__new__(PipelineLimit)
        object.__setattr__(limit, "limit", 10)
        object.__setattr__(limit, "kind", "concurrent")  # str, not LimitKind
        object.__setattr__(limit, "timeout", 600)
        with pytest.raises(TypeError, match="kind must be LimitKind"):
            _validate_concurrency_limits("TestDeploy", {"test": limit})

    @pytest.mark.ai_docs
    def test_name_with_dashes_and_underscores(self):
        raw = {"my-limit_v2": PipelineLimit(10)}
        result = _validate_concurrency_limits("TestDeploy", raw)
        assert "my-limit_v2" in result

    def test_returns_immutable(self):
        result = _validate_concurrency_limits("TestDeploy", {"a": PipelineLimit(10)})
        with pytest.raises(TypeError):
            result["new"] = PipelineLimit(5)  # type: ignore[index]


# ---------------------------------------------------------------------------
# ContextVar helpers
# ---------------------------------------------------------------------------


class TestContextVarHelpers:
    def test_set_and_restore(self):
        original = _limits_state.get()
        new_state = _LimitsState(limits=MappingProxyType({"x": PipelineLimit(1)}), status=_SharedStatus())
        with _set_limits_state(new_state):
            assert _limits_state.get() is new_state
        assert _limits_state.get() is original


# ---------------------------------------------------------------------------
# pipeline_concurrency — KeyError for unregistered limit
# ---------------------------------------------------------------------------


class TestPipelineConcurrencyKeyError:
    async def test_unregistered_limit_raises_key_error(self):
        state = _LimitsState(limits=MappingProxyType({}), status=_SharedStatus())
        with _set_limits_state(state):
            with pytest.raises(KeyError, match="not registered"):
                async with pipeline_concurrency("nonexistent"):
                    pass

    async def test_key_error_shows_available_limits(self):
        limits = {"alpha": PipelineLimit(10), "beta": PipelineLimit(20)}
        state = _LimitsState(limits=MappingProxyType(limits), status=_SharedStatus())
        with _set_limits_state(state):
            with pytest.raises(KeyError, match="alpha, beta"):
                async with pipeline_concurrency("missing"):
                    pass


# ---------------------------------------------------------------------------
# pipeline_concurrency — local semaphore fallback
# ---------------------------------------------------------------------------


class TestPipelineConcurrencyLocalFallback:
    async def test_concurrent_uses_semaphore_when_prefect_unavailable(self):
        status = _SharedStatus()
        status.prefect_available = False
        limits = {"test": PipelineLimit(2, LimitKind.CONCURRENT, timeout=5)}
        state = _LimitsState(limits=MappingProxyType(limits), status=status)
        with _set_limits_state(state):
            entered = False
            async with pipeline_concurrency("test"):
                entered = True
            assert entered

    async def test_per_minute_no_local_fallback(self):
        """PER_MINUTE with Prefect unavailable just yields (no semaphore)."""
        status = _SharedStatus()
        status.prefect_available = False
        limits = {"rate": PipelineLimit(10, LimitKind.PER_MINUTE)}
        state = _LimitsState(limits=MappingProxyType(limits), status=status)
        with _set_limits_state(state):
            entered = False
            async with pipeline_concurrency("rate"):
                entered = True
            assert entered

    async def test_per_hour_no_local_fallback(self):
        """PER_HOUR with Prefect unavailable just yields (no semaphore)."""
        status = _SharedStatus()
        status.prefect_available = False
        limits = {"rate": PipelineLimit(10, LimitKind.PER_HOUR)}
        state = _LimitsState(limits=MappingProxyType(limits), status=status)
        with _set_limits_state(state):
            entered = False
            async with pipeline_concurrency("rate"):
                entered = True
            assert entered


# ---------------------------------------------------------------------------
# pipeline_concurrency — Prefect integration (mocked)
# ---------------------------------------------------------------------------


class TestPipelineConcurrencyPrefect:
    async def test_concurrent_calls_prefect_concurrency(self):
        status = _SharedStatus()
        limits = {"bd": PipelineLimit(500, LimitKind.CONCURRENT, timeout=60)}
        state = _LimitsState(limits=MappingProxyType(limits), status=status)
        with _set_limits_state(state):
            with patch("ai_pipeline_core.pipeline.limits.concurrency") as mock_cm:
                mock_cm.return_value.__aenter__ = AsyncMock()
                mock_cm.return_value.__aexit__ = AsyncMock(return_value=False)
                async with pipeline_concurrency("bd"):
                    pass
                mock_cm.assert_called_once_with("bd", occupy=1, timeout_seconds=60, strict=False)

    async def test_per_minute_calls_rate_limit(self):
        status = _SharedStatus()
        limits = {"sf": PipelineLimit(15, LimitKind.PER_MINUTE, timeout=300)}
        state = _LimitsState(limits=MappingProxyType(limits), status=status)
        with _set_limits_state(state):
            with patch("ai_pipeline_core.pipeline.limits.rate_limit", new_callable=AsyncMock) as mock_rl:
                async with pipeline_concurrency("sf"):
                    pass
                mock_rl.assert_called_once_with("sf", occupy=1, timeout_seconds=300, strict=False)

    async def test_per_hour_calls_rate_limit(self):
        status = _SharedStatus()
        limits = {"hourly": PipelineLimit(100, LimitKind.PER_HOUR, timeout=120)}
        state = _LimitsState(limits=MappingProxyType(limits), status=status)
        with _set_limits_state(state):
            with patch("ai_pipeline_core.pipeline.limits.rate_limit", new_callable=AsyncMock) as mock_rl:
                async with pipeline_concurrency("hourly"):
                    pass
                mock_rl.assert_called_once_with("hourly", occupy=1, timeout_seconds=120, strict=False)

    async def test_timeout_override(self):
        status = _SharedStatus()
        limits = {"bd": PipelineLimit(500, timeout=600)}
        state = _LimitsState(limits=MappingProxyType(limits), status=status)
        with _set_limits_state(state):
            with patch("ai_pipeline_core.pipeline.limits.concurrency") as mock_cm:
                mock_cm.return_value.__aenter__ = AsyncMock()
                mock_cm.return_value.__aexit__ = AsyncMock(return_value=False)
                async with pipeline_concurrency("bd", timeout=30):
                    pass
                mock_cm.assert_called_once_with("bd", occupy=1, timeout_seconds=30, strict=False)

    async def test_timeout_error_propagates(self):
        status = _SharedStatus()
        limits = {"bd": PipelineLimit(500)}
        state = _LimitsState(limits=MappingProxyType(limits), status=status)
        with _set_limits_state(state):
            with patch("ai_pipeline_core.pipeline.limits.concurrency") as mock_cm:
                mock_cm.return_value.__aenter__ = AsyncMock(side_effect=AcquireConcurrencySlotTimeoutError("timeout"))
                with pytest.raises(AcquireConcurrencySlotTimeoutError):
                    async with pipeline_concurrency("bd"):
                        pass
            # prefect_available should still be True
            assert status.prefect_available is True

    async def test_acquisition_error_degrades_to_local_concurrent(self):
        """ConcurrencySlotAcquisitionError should degrade to local semaphore for CONCURRENT."""
        status = _SharedStatus()
        limits = {"bd": PipelineLimit(5, LimitKind.CONCURRENT, timeout=5)}
        state = _LimitsState(limits=MappingProxyType(limits), status=status)
        with _set_limits_state(state):
            with patch("ai_pipeline_core.pipeline.limits.concurrency") as mock_cm:
                mock_cm.return_value.__aenter__ = AsyncMock(side_effect=ConcurrencySlotAcquisitionError("server error"))
                entered = False
                async with pipeline_concurrency("bd"):
                    entered = True
                assert entered
                assert status.prefect_available is False

    async def test_acquisition_error_degrades_to_noop_for_rate_limit(self):
        """ConcurrencySlotAcquisitionError should yield for PER_MINUTE (no local fallback)."""
        status = _SharedStatus()
        limits = {"sf": PipelineLimit(15, LimitKind.PER_MINUTE)}
        state = _LimitsState(limits=MappingProxyType(limits), status=status)
        with _set_limits_state(state):
            with patch("ai_pipeline_core.pipeline.limits.rate_limit", new_callable=AsyncMock) as mock_rl:
                mock_rl.side_effect = ConcurrencySlotAcquisitionError("server error")
                entered = False
                async with pipeline_concurrency("sf"):
                    entered = True
                assert entered
                assert status.prefect_available is False


# ---------------------------------------------------------------------------
# pipeline_concurrency — shared status across asyncio.gather
# ---------------------------------------------------------------------------


class TestSharedStatusAcrossGather:
    async def test_prefect_failure_visible_to_sibling_tasks(self):
        """When one gather branch sets prefect_available=False, siblings see it."""
        status = _SharedStatus()
        limits = {"bd": PipelineLimit(5, LimitKind.CONCURRENT, timeout=5)}
        state = _LimitsState(limits=MappingProxyType(limits), status=status)
        with _set_limits_state(state):
            call_count = 0

            async def worker_that_triggers_degradation():
                nonlocal call_count
                with patch("ai_pipeline_core.pipeline.limits.concurrency") as mock_cm:
                    mock_cm.return_value.__aenter__ = AsyncMock(side_effect=ConcurrencySlotAcquisitionError("down"))
                    async with pipeline_concurrency("bd"):
                        call_count += 1

            async def worker_that_checks_status():
                # Small delay so the first worker degrades first
                await asyncio.sleep(0.05)
                # By now, prefect_available should be False
                assert status.prefect_available is False
                call_count_before = call_count
                async with pipeline_concurrency("bd"):
                    pass
                # This worker used local semaphore, not Prefect
                assert call_count == call_count_before

            await asyncio.gather(worker_that_triggers_degradation(), worker_that_checks_status())


# ---------------------------------------------------------------------------
# _ensure_concurrency_limits
# ---------------------------------------------------------------------------


class TestEnsureConcurrencyLimits:
    async def test_empty_limits_is_noop(self):
        """Empty limits should not call Prefect at all."""
        await _ensure_concurrency_limits(MappingProxyType({}))

    async def test_upserts_all_limits(self):
        limits: Mapping[str, PipelineLimit] = {
            "bd": PipelineLimit(500, LimitKind.CONCURRENT),
            "sf": PipelineLimit(15, LimitKind.PER_MINUTE),
            "hourly": PipelineLimit(3600, LimitKind.PER_HOUR),
        }
        status = _SharedStatus()
        state = _LimitsState(limits=MappingProxyType(limits), status=status)
        with _set_limits_state(state):
            mock_client = AsyncMock()
            with patch("ai_pipeline_core.pipeline.limits.get_client") as mock_get_client:
                mock_get_client.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_get_client.return_value.__aexit__ = AsyncMock(return_value=False)
                await _ensure_concurrency_limits(limits)

            assert mock_client.upsert_global_concurrency_limit_by_name.call_count == 3
            calls = {call.kwargs["name"]: call.kwargs for call in mock_client.upsert_global_concurrency_limit_by_name.call_args_list}
            assert calls["bd"]["limit"] == 500
            assert calls["bd"]["slot_decay_per_second"] == pytest.approx(0.0)
            assert calls["sf"]["limit"] == 15
            assert calls["sf"]["slot_decay_per_second"] == pytest.approx(0.25)
            assert calls["hourly"]["limit"] == 3600
            assert calls["hourly"]["slot_decay_per_second"] == pytest.approx(1.0)
            assert status.prefect_available is True

    async def test_prefect_failure_degrades(self):
        limits: Mapping[str, PipelineLimit] = {"bd": PipelineLimit(500)}
        status = _SharedStatus()
        state = _LimitsState(limits=MappingProxyType(limits), status=status)
        with _set_limits_state(state):
            with patch("ai_pipeline_core.pipeline.limits.get_client") as mock_get_client:
                mock_get_client.return_value.__aenter__ = AsyncMock(side_effect=ConnectionError("no server"))
                await _ensure_concurrency_limits(limits)
            assert status.prefect_available is False
