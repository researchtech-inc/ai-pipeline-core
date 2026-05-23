"""Unit tests for the three-gate streaming watchdog primitive."""

import time

import pytest

from ai_pipeline_core._llm_core._watchdog import StreamWatchdog, WatchdogConfig
from ai_pipeline_core._llm_core.exceptions import StreamWatchdogError


def _fast_config(**overrides: float) -> WatchdogConfig:
    defaults = {
        "ttft_seconds": 0.05,
        "inactivity_seconds": 0.05,
        "total_wall_seconds": 5.0,
        "tick_seconds": 0.01,
    }
    defaults.update(overrides)
    return WatchdogConfig(**defaults)


def test_ttft_gate_fires_when_no_first_chunk_arrives() -> None:
    watchdog = StreamWatchdog(deployment_id="dep-ttft", config=_fast_config())
    watchdog.start()

    time.sleep(0.08)

    with pytest.raises(StreamWatchdogError) as exc_info:
        watchdog.check()
    assert exc_info.value.reason == "ttft"
    assert exc_info.value.deployment_id == "dep-ttft"
    assert exc_info.value.observed_tps == 0.0


def test_stall_gate_fires_after_inactivity_post_first_content() -> None:
    watchdog = StreamWatchdog(deployment_id="dep-stall", config=_fast_config())
    watchdog.start()
    watchdog.on_content(8)

    time.sleep(0.08)

    with pytest.raises(StreamWatchdogError) as exc_info:
        watchdog.check()
    assert exc_info.value.reason == "stall"
    assert exc_info.value.deployment_id == "dep-stall"


def test_total_gate_fires_after_overall_wall_clock_budget() -> None:
    watchdog = StreamWatchdog(
        deployment_id="dep-total",
        config=_fast_config(ttft_seconds=10.0, inactivity_seconds=10.0, total_wall_seconds=0.05),
    )
    watchdog.start()
    watchdog.on_content(4)

    time.sleep(0.08)

    with pytest.raises(StreamWatchdogError) as exc_info:
        watchdog.check()
    assert exc_info.value.reason == "total"


def test_bursty_stream_with_gaps_below_inactivity_threshold_does_not_fire() -> None:
    """Bursts of content separated by gaps below ``inactivity_seconds`` are healthy.

    Regression guard for the AtlasCloud/Parasail false-positive kills: the
    old leaky-bucket TPS check tripped when inter-burst gaps drained the
    rolling window, even when each gap was well under any timer threshold.
    """
    watchdog = StreamWatchdog(
        deployment_id="dep-bursty",
        config=_fast_config(inactivity_seconds=0.2, ttft_seconds=0.2, total_wall_seconds=5.0),
    )
    watchdog.start()
    for _ in range(3):
        watchdog.on_content(20)
        watchdog.check()
        time.sleep(0.05)
    watchdog.on_content(20)
    watchdog.check()


def test_start_anchors_clock_after_construction_delay() -> None:
    """Wall-clock measurement begins at ``start()`` so warmup time does not eat the budget."""
    watchdog = StreamWatchdog(
        deployment_id="dep-late-start",
        config=_fast_config(total_wall_seconds=0.2),
    )

    time.sleep(0.25)
    watchdog.start()
    watchdog.check()  # no raise — total budget is anchored to start(), not construction


def test_disabled_watchdog_never_raises() -> None:
    watchdog = StreamWatchdog(enabled=False, config=_fast_config())
    watchdog.start()

    time.sleep(0.08)

    watchdog.check()


def test_check_before_start_is_a_noop() -> None:
    """Before ``start()`` is called the clock is not anchored; ``check()`` must be safe."""
    watchdog = StreamWatchdog(config=_fast_config())

    watchdog.check()
    watchdog.on_content(10)
    watchdog.check()


def test_final_tps_reports_observed_throughput() -> None:
    watchdog = StreamWatchdog(config=_fast_config(total_wall_seconds=5.0))
    watchdog.start()
    watchdog.on_content(100)
    time.sleep(0.02)

    assert watchdog.final_tps() > 0
