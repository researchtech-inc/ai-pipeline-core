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
    watchdog.on_content(text_tokens=8)

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
    watchdog.on_content(text_tokens=4)

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
        watchdog.on_content(text_tokens=20)
        watchdog.check()
        time.sleep(0.05)
    watchdog.on_content(text_tokens=20)
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
    watchdog.on_content(text_tokens=10)
    watchdog.check()


def test_final_tps_reports_observed_throughput() -> None:
    watchdog = StreamWatchdog(config=_fast_config(total_wall_seconds=5.0))
    watchdog.start()
    watchdog.on_content(text_tokens=100)
    time.sleep(0.02)

    assert watchdog.final_tps() > 0


def test_tool_call_chunk_relaxes_inactivity_gate() -> None:
    """A gap that exceeds ``inactivity_seconds`` but stays under the relaxed
    ``tool_call_inactivity_seconds`` budget must NOT fire after a tool-call
    delta. Models the search-models wire shape where lifecycle keepalive
    deltas arrive 5-30s apart instead of the strict 30s text-burst cadence.
    """
    watchdog = StreamWatchdog(
        deployment_id="dep-tool",
        config=_fast_config(inactivity_seconds=0.05, tool_call_inactivity_seconds=5.0),
    )
    watchdog.start()
    watchdog.on_content(tool_call_tokens=1)

    time.sleep(0.1)

    watchdog.check()  # no raise — tool-call gate is 5s


def test_tool_call_inactivity_gate_fires_after_its_own_budget() -> None:
    """Even with the relaxed gate, silence beyond ``tool_call_inactivity_seconds``
    is still a stall. Worst-case bound preserved.
    """
    watchdog = StreamWatchdog(
        deployment_id="dep-tool-stall",
        config=_fast_config(inactivity_seconds=10.0, tool_call_inactivity_seconds=0.05),
    )
    watchdog.start()
    watchdog.on_content(tool_call_tokens=1)

    time.sleep(0.1)

    with pytest.raises(StreamWatchdogError) as exc_info:
        watchdog.check()
    assert exc_info.value.reason == "stall"


def test_text_chunk_after_tool_call_snaps_back_to_strict_gate() -> None:
    """Once the model emits actual text, the strict inactivity gate applies
    again. Prevents a model from coasting on a single tool-call delta and
    then silently stalling forever.
    """
    watchdog = StreamWatchdog(
        deployment_id="dep-snap",
        config=_fast_config(inactivity_seconds=0.05, tool_call_inactivity_seconds=5.0),
    )
    watchdog.start()
    watchdog.on_content(tool_call_tokens=1)
    watchdog.on_content(text_tokens=4)

    time.sleep(0.1)

    with pytest.raises(StreamWatchdogError) as exc_info:
        watchdog.check()
    assert exc_info.value.reason == "stall"


def test_mixed_chunk_with_text_classifies_as_strict_mode() -> None:
    """A chunk carrying both text and tool_call tokens is text-dominant —
    the strict inactivity gate applies on the next check.
    """
    watchdog = StreamWatchdog(
        config=_fast_config(inactivity_seconds=0.05, tool_call_inactivity_seconds=5.0),
    )
    watchdog.start()
    watchdog.on_content(text_tokens=3, tool_call_tokens=1)

    time.sleep(0.1)

    with pytest.raises(StreamWatchdogError):
        watchdog.check()


def test_reasoning_chunks_satisfy_the_ttft_gate() -> None:
    """A stream that emits only reasoning_content within the TTFT budget
    must NOT be killed. Reasoning is committed model output and proves the
    upstream is alive — the watchdog's job is liveness, not productivity.
    Regression for PDF + reasoning_effort=high which sends reasoning chunks
    for tens of seconds before any visible text.
    """
    watchdog = StreamWatchdog(
        config=_fast_config(ttft_seconds=0.1, inactivity_seconds=5.0, total_wall_seconds=5.0),
    )
    watchdog.start()
    watchdog.on_content(reasoning_tokens=8)

    time.sleep(0.2)

    watchdog.check()  # no raise — reasoning satisfied TTFT


def test_reasoning_chunks_satisfy_the_inactivity_gate() -> None:
    """After the first chunk, a steady stream of reasoning chunks keeps the
    strict inactivity gate satisfied.
    """
    watchdog = StreamWatchdog(
        config=_fast_config(inactivity_seconds=0.1, total_wall_seconds=5.0),
    )
    watchdog.start()
    watchdog.on_content(reasoning_tokens=4)
    time.sleep(0.05)
    watchdog.on_content(reasoning_tokens=4)
    time.sleep(0.05)
    watchdog.on_content(reasoning_tokens=4)
    watchdog.check()  # no raise


def test_reasoning_does_not_flip_to_relaxed_tool_call_gate() -> None:
    """A pure-reasoning chunk uses the strict inactivity gate, not the relaxed
    tool-call gate. Reasoning streams should be chatty enough to satisfy 30s.
    """
    watchdog = StreamWatchdog(
        config=_fast_config(inactivity_seconds=0.05, tool_call_inactivity_seconds=5.0),
    )
    watchdog.start()
    watchdog.on_content(reasoning_tokens=4)

    time.sleep(0.1)

    with pytest.raises(StreamWatchdogError) as exc_info:
        watchdog.check()
    assert exc_info.value.reason == "stall"


def test_text_then_tool_call_then_text_toggles_strict_mode() -> None:
    """The relaxed gate flag toggles on every chunk. A `text → tool_call → text`
    sequence ends in strict mode and a gap > strict inactivity raises.
    """
    watchdog = StreamWatchdog(
        config=_fast_config(inactivity_seconds=0.05, tool_call_inactivity_seconds=5.0),
    )
    watchdog.start()
    watchdog.on_content(text_tokens=4)
    watchdog.on_content(tool_call_tokens=1)
    watchdog.on_content(text_tokens=4)

    time.sleep(0.1)

    with pytest.raises(StreamWatchdogError) as exc_info:
        watchdog.check()
    assert exc_info.value.reason == "stall"


def test_relaxed_inactivity_gate_does_not_extend_total_budget() -> None:
    """The relaxed inactivity gate must NOT shadow the total-wall budget.
    A stream that emits a tool_call every few seconds for longer than
    ``total_wall_seconds`` is still killed by the total gate.
    """
    watchdog = StreamWatchdog(
        config=_fast_config(
            ttft_seconds=10.0,
            inactivity_seconds=10.0,
            tool_call_inactivity_seconds=300.0,
            total_wall_seconds=0.05,
        ),
    )
    watchdog.start()
    watchdog.on_content(tool_call_tokens=1)

    time.sleep(0.1)

    with pytest.raises(StreamWatchdogError) as exc_info:
        watchdog.check()
    assert exc_info.value.reason == "total"
