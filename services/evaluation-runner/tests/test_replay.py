"""
End-to-end test of the scoring pipeline over synthetic telemetry.

Runs the real detector, the real grouper and the real scoring code against a
generated metric stream with a known fault in it, so the whole chain is
exercised without needing the Docker stack up. If this passes, the pieces fit
together; whether they work against the live testbed is a separate question
that only a `live` run can answer.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import replay as replaying  # noqa: E402
from scoring import (  # noqa: E402
    Outcome,
    Scenario,
    find_false_positives,
    quiet_seconds,
    score_scenario,
    summarize,
)

BASE = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)
SERVICES = ["front-end", "catalogue", "payment", "user", "carts"]
SAMPLE_INTERVAL = 5

# Deterministic wobble, so a failure means a real regression rather than an
# unlucky seed.
WOBBLE = [0.0, 1.2, -0.8, 0.5, -1.5, 0.9, -0.4, 1.8, -1.1, 0.3]


def healthy(metric: str, service: str, tick: int) -> float:
    jitter = WOBBLE[tick % len(WOBBLE)]
    return {
        "latency_p50_ms": 20.0 + jitter,
        "latency_p95_ms": 36.0 + jitter * 2,
        "latency_p99_ms": 48.0 + jitter * 3,
        "error_rate": 0.0,
        "cpu_rate": 0.15 + jitter * 0.01,
        "memory_bytes": 2.0e8 + jitter * 1e6,
    }[metric]


METRICS = ["latency_p50_ms", "latency_p95_ms", "latency_p99_ms",
            "error_rate", "cpu_rate", "memory_bytes"]


def build_stream(fault_service=None, fault_from=None, fault_to=None, ticks=360):
    """Generate (time, service, metric, value) rows like TimescaleDB returns."""
    samples = []
    for tick in range(ticks):
        at = BASE + timedelta(seconds=tick * SAMPLE_INTERVAL)
        for service in SERVICES:
            faulty = (
                service == fault_service
                and fault_from is not None
                and fault_from <= at <= fault_to
            )
            for metric in METRICS:
                value = healthy(metric, service, tick)
                if faulty:
                    # A CPU-throttled service: latency explodes, errors appear.
                    if metric.startswith("latency"):
                        value *= 120.0
                    elif metric == "error_rate":
                        value = 0.35
                    elif metric == "cpu_rate":
                        value = 0.98
                samples.append((at, service, metric, value))
    return samples


def a_scenario(service="catalogue", start_tick=120, end_tick=140):
    return Scenario(
        scenario_id="scn-test-1",
        fault_type="bad_deploy_latency",
        ground_truth_service=service,
        t_inject=BASE + timedelta(seconds=start_tick * SAMPLE_INTERVAL),
        t_recovered=BASE + timedelta(seconds=end_tick * SAMPLE_INTERVAL),
    )


@pytest.mark.parametrize("detector", ["ewma", "zscore", "cusum", "static"])
def test_every_detector_finds_an_obvious_fault(detector):
    scenario = a_scenario()
    samples = build_stream("catalogue", scenario.t_inject, scenario.t_recovered)

    events = replaying.replay(samples, detector)
    result = score_scenario(scenario, events)

    assert result.outcome is Outcome.DETECTED, f"{detector} missed an obvious fault"
    assert result.detection_latency_s is not None
    assert result.detection_latency_s < 60


def test_a_quiet_stream_produces_no_alerts():
    """The false-positive floor: no fault, no alerts."""
    samples = build_stream()

    for detector in ("ewma", "zscore", "cusum", "static"):
        events = replaying.replay(samples, detector)
        assert events == [], f"{detector} alerted on a healthy stream"


def test_one_fault_produces_one_event_not_one_per_metric():
    """A fault trips latency, errors and CPU at once — still one incident."""
    scenario = a_scenario()
    samples = build_stream("catalogue", scenario.t_inject, scenario.t_recovered)

    events = replaying.replay(samples, "ewma")

    assert len(events) == 1
    assert "catalogue" in events[0].services
    assert len(events[0].metrics) > 1, "expected several metrics on one event"


def test_replay_is_deterministic():
    """Ablation numbers are only meaningful if a rerun reproduces them."""
    scenario = a_scenario()
    samples = build_stream("catalogue", scenario.t_inject, scenario.t_recovered)

    first = replaying.replay(samples, "ewma")
    second = replaying.replay(samples, "ewma")

    assert [e.anomaly_id for e in first] == [e.anomaly_id for e in second]
    assert [e.t_detected for e in first] == [e.t_detected for e in second]


def test_a_fault_on_one_service_is_not_blamed_on_another():
    scenario = a_scenario(service="payment")
    samples = build_stream("catalogue", scenario.t_inject, scenario.t_recovered)

    result = score_scenario(scenario, replaying.replay(samples, "ewma"))

    assert result.outcome is Outcome.MISATTRIBUTED


def test_scoring_a_full_run_end_to_end():
    scenario = a_scenario()
    samples = build_stream("catalogue", scenario.t_inject, scenario.t_recovered)
    events = replaying.replay(samples, "ewma")

    results = [score_scenario(scenario, events)]
    summaries = summarize(results)
    false_positives = find_false_positives(events, [scenario])
    quiet = quiet_seconds(samples[0][0], samples[-1][0], [scenario])

    assert summaries["overall"].detected == 1
    assert summaries["bad_deploy_latency"].detection_rate == 1.0
    assert false_positives == []
    assert quiet > 0


def build_crash_stream(crashed_service, crash_from, crash_to, ticks=360):
    """A crashed service vanishes from the stream rather than reporting badly.

    This mirrors what actually happens: metrics-bridge only publishes targets
    Prometheus reports as `up`, so a stopped container produces no samples at
    all. The first live run missed exactly this.
    """
    samples = []
    for tick in range(ticks):
        at = BASE + timedelta(seconds=tick * SAMPLE_INTERVAL)
        for service in SERVICES:
            if service == crashed_service and crash_from <= at <= crash_to:
                continue
            for metric in METRICS:
                samples.append((at, service, metric, healthy(metric, service, tick)))
    return samples


def test_a_crashed_service_is_detected_even_though_it_sends_nothing():
    scenario = Scenario(
        scenario_id="scn-crash-1",
        fault_type="service_crash",
        ground_truth_service="payment",
        t_inject=BASE + timedelta(seconds=120 * SAMPLE_INTERVAL),
        t_recovered=BASE + timedelta(seconds=140 * SAMPLE_INTERVAL),
    )
    samples = build_crash_stream("payment", scenario.t_inject, scenario.t_recovered)

    events = replaying.replay(samples, "ewma")
    result = score_scenario(scenario, events)

    assert result.outcome is Outcome.DETECTED
    assert result.detection_latency_s < 60


def test_a_stream_with_no_gaps_raises_no_liveness_alert():
    samples = build_stream()
    assert replaying.replay(samples, "ewma") == []


def build_restart_stream(fault_from_tick, fault_to_tick, ticks=120):
    """What the 2026-09-14 smoke run actually saw on catalogue.

    After a stack restart catalogue was slow for its first minute while it
    booted, then settled at ~5 ms, then took a 362 ms CPU-throttle fault.
    """
    boot = {"latency_p50_ms": 30.0, "latency_p95_ms": 39.0, "latency_p99_ms": 47.0}
    settled = {"latency_p50_ms": 2.5, "latency_p95_ms": 5.0, "latency_p99_ms": 6.0}
    fault = {"latency_p50_ms": 150.0, "latency_p95_ms": 362.5, "latency_p99_ms": 472.5}

    samples = []
    for tick in range(ticks):
        at = BASE + timedelta(seconds=tick * SAMPLE_INTERVAL)
        jitter = WOBBLE[tick % len(WOBBLE)]
        for service in SERVICES:
            for metric in METRICS:
                value = healthy(metric, service, tick)
                if service == "catalogue" and metric in boot:
                    if tick < 12:
                        value = boot[metric] + jitter
                    elif fault_from_tick <= tick <= fault_to_tick:
                        value = fault[metric]
                    else:
                        value = settled[metric] + jitter * 0.1
                samples.append((at, service, metric, value))
    return samples


@pytest.mark.parametrize("detector", ["ewma", "zscore", "cusum"])
def test_a_slow_boot_neither_false_alarms_nor_masks_a_later_fault(detector):
    """Regression for the 2026-09-14 miss, end to end.

    The detector alerted when latency fell back to normal after boot, and
    that false alarm's cooldown then swallowed the real fault. `static` is
    left out: 362 ms is under its 500 ms limit, so it misses by design.
    """
    scenario = a_scenario(start_tick=42, end_tick=54)
    samples = build_restart_stream(42, 54)

    events = replaying.replay(samples, detector)

    assert find_false_positives(events, [scenario]) == []
    assert score_scenario(scenario, events).outcome is Outcome.DETECTED


def test_a_deploy_window_raises_the_bar_without_hiding_a_real_regression():
    """The bad-deploy case must still be caught while its deploy is recent."""
    scenario = a_scenario()
    samples = build_stream("catalogue", scenario.t_inject, scenario.t_recovered)
    deploys = [("dep-test-0001", "catalogue", scenario.t_inject - timedelta(seconds=10))]

    events = replaying.replay(samples, "ewma", deploys=deploys)
    result = score_scenario(scenario, events)

    assert result.outcome is Outcome.DETECTED
