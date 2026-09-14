import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from detectors import (  # noqa: E402
    CUSUMDetector,
    EWMADetector,
    StaticThresholdDetector,
    ThreeSigmaDetector,
    build_detector,
)


def ts(second: int) -> str:
    return f"2026-09-13T12:{second // 60:02d}:{second % 60:02d}.000Z"


def feed(detector, values, start=0):
    """Push values in and return every signal produced."""
    signals = []
    for i, value in enumerate(values):
        signal = detector.update(float(value), ts(start + i * 5))
        if signal is not None:
            signals.append(signal)
    return signals


QUIET_LATENCY = [36.0, 36.4, 35.7, 36.1, 36.3, 35.9, 36.2, 35.8, 36.0, 36.1, 36.2, 35.9]


def test_ewma_detects_sustained_step_change():
    detector = EWMADetector("catalogue", "latency_p95_ms", warmup=5)

    assert feed(detector, QUIET_LATENCY) == []

    signals = feed(detector, [7470.0, 7480.0], start=100)

    assert len(signals) == 1
    assert signals[0].service == "catalogue"
    assert signals[0].severity == "high"
    assert signals[0].baseline == pytest.approx(36.0, abs=1.0)


def test_ewma_stays_quiet_on_a_healthy_stream():
    detector = EWMADetector("catalogue", "latency_p95_ms", warmup=5)
    assert feed(detector, QUIET_LATENCY * 5) == []


def test_single_spike_does_not_fire_when_corroboration_required():
    """One bad sample is noise; the detector must wait for a second."""
    detector = EWMADetector("catalogue", "latency_p95_ms", warmup=5, required_breaches=2)
    feed(detector, QUIET_LATENCY)

    assert feed(detector, [9000.0], start=100) == []
    assert len(feed(detector, [36.0, 36.1], start=200)) == 0


def test_no_signal_during_warmup():
    detector = EWMADetector("catalogue", "latency_p95_ms", warmup=10)
    assert feed(detector, [36.0, 5000.0, 6000.0]) == []
    assert not detector.warm


def test_error_rate_can_fire_despite_its_tiny_scale():
    """Regression test: an absolute noise floor made this unreachable.

    error_rate lives in [0, 1]. With a fixed std floor of 0.5, a 3-sigma
    breach needed an error rate above 1.5 — impossible — so error-rate
    anomalies could never fire at all.
    """
    detector = EWMADetector("catalogue", "error_rate", warmup=5)
    feed(detector, [0.001] * 12)

    signals = feed(detector, [0.42, 0.45], start=100)

    assert len(signals) == 1
    assert signals[0].metric == "error_rate"


def test_negligible_wobble_on_a_near_zero_metric_is_ignored():
    """Statistically striking, practically meaningless — must not fire."""
    detector = EWMADetector("catalogue", "error_rate", warmup=5)
    feed(detector, [0.0] * 12)

    # A jump from 0 to 0.002 is an enormous z-score against zero variance,
    # but it is a fifth of a percentage point and nobody should be paged.
    assert feed(detector, [0.002, 0.002, 0.002], start=100) == []


def test_baseline_does_not_absorb_an_ongoing_fault():
    """A sustained fault must not quietly become the new normal."""
    detector = EWMADetector("catalogue", "latency_p95_ms", warmup=5)
    feed(detector, QUIET_LATENCY)

    signals = feed(detector, [7470.0] * 8, start=100)

    assert signals, "detector went silent while the fault was still running"
    assert detector._baseline.mean == pytest.approx(36.0, abs=5.0)


# Catalogue's p95 as seen after a stack restart on 2026-09-14: slow for its
# first minute while it booted, then settled at its real ~5 ms level.
BOOT_LATENCY = [39.0, 38.5, 39.4, 38.8, 39.1, 39.3, 38.7, 39.0, 38.9, 39.2]
SETTLED_LATENCY = [5.0, 4.9, 5.1, 5.0, 4.8, 5.2, 5.0, 4.9, 5.1, 5.0] * 3


@pytest.mark.parametrize("kind", ["ewma", "zscore", "cusum"])
def test_latency_falling_back_to_normal_is_not_an_anomaly(kind):
    """Regression: a service recovering from a slow boot raised a false alarm.

    The detector learned the cold-start latency as its baseline, then alerted
    when latency dropped back to normal. Faster is never an incident for
    latency, and that false alarm went on to mute a real fault.
    """
    detector = build_detector(kind, service="catalogue", metric="latency_p95_ms", warmup=10)
    feed(detector, BOOT_LATENCY)

    assert feed(detector, SETTLED_LATENCY, start=100) == []


@pytest.mark.parametrize("kind", ["ewma", "zscore", "cusum"])
def test_a_fault_after_a_slow_boot_is_still_caught(kind):
    """The baseline must follow latency down, not stay anchored at boot level."""
    detector = build_detector(kind, service="catalogue", metric="latency_p95_ms", warmup=10)
    feed(detector, BOOT_LATENCY)
    feed(detector, SETTLED_LATENCY, start=100)

    signals = feed(detector, [362.5] * 4, start=400)

    assert signals, f"{kind} missed a 70x latency regression after a slow boot"
    assert signals[0].baseline < 20


def test_error_rate_falling_is_not_an_anomaly():
    detector = EWMADetector("catalogue", "error_rate", warmup=5)
    feed(detector, [0.30] * 12)

    assert feed(detector, [0.0, 0.0, 0.0], start=100) == []


def test_memory_drop_is_still_reported():
    """Only latency and error rate are one-directional.

    A sudden fall in memory usually means the process restarted, which is
    worth knowing about.
    """
    detector = EWMADetector("carts", "memory_bytes", warmup=5)
    feed(detector, [4.0e8 + i * 1e5 for i in range(12)])

    assert len(feed(detector, [1.0e8, 1.0e8], start=100)) == 1


def test_static_threshold_fires_only_above_its_limit():
    detector = StaticThresholdDetector("catalogue", "latency_p95_ms")

    assert feed(detector, [400.0, 450.0, 499.0]) == []
    assert len(feed(detector, [600.0, 700.0], start=100)) == 1


def test_static_threshold_ignores_metrics_it_has_no_limit_for():
    detector = StaticThresholdDetector("catalogue", "request_rate")
    assert feed(detector, [1.0, 500.0, 99999.0]) == []


def first_firing_index(detector, values) -> int | None:
    for i, value in enumerate(values):
        if detector.update(float(value), ts(200 + i * 5)) is not None:
            return i
    return None


# A fixed, deterministic noise pattern (std ~2.1) rather than a seeded RNG,
# so these results cannot shift under a different Python build.
NOISE = [0.0, 2.5, -3.0, 1.5, -2.0, 3.0, -1.5, 0.5, -2.5, 2.0]


def test_cusum_catches_a_slow_drift_that_a_z_score_misses():
    """The reason CUSUM is in the comparison set: gradual degradation.

    A memory leak creeping up at a fifth of the noise amplitude never makes
    any single sample look extreme, so a per-sample z-score never trips.
    CUSUM accumulates the evidence across samples and catches it.
    """
    quiet = [100.0 + NOISE[i % len(NOISE)] for i in range(60)]
    leak = [100.0 + i * 0.4 + NOISE[i % len(NOISE)] for i in range(1, 60)]

    cusum = CUSUMDetector("carts", "memory_bytes", warmup=40, min_deviation=0.0)
    zscore = ThreeSigmaDetector("carts", "memory_bytes", warmup=40, window=40,
                                min_deviation=0.0)

    feed(cusum, quiet)
    feed(zscore, quiet)

    assert first_firing_index(cusum, leak) is not None
    assert first_firing_index(zscore, leak) is None


def test_both_statistical_detectors_catch_an_abrupt_jump_promptly():
    """Neither statistic should need long for the easy case.

    Note the asymmetry when reading ablation numbers: CUSUM is configured to
    fire on a single threshold crossing (its statistic already encodes
    persistence), while the z-score waits for corroboration, so CUSUM can
    report one sample sooner on an abrupt change. That is a configuration
    difference, not evidence that one statistic is better.
    """
    quiet = [100.0 + NOISE[i % len(NOISE)] for i in range(60)]
    spike = [180.0 + NOISE[i % len(NOISE)] for i in range(20)]

    cusum = CUSUMDetector("carts", "memory_bytes", warmup=40, min_deviation=0.0)
    zscore = ThreeSigmaDetector("carts", "memory_bytes", warmup=40, window=40,
                                min_deviation=0.0)

    feed(cusum, quiet)
    feed(zscore, quiet)

    assert first_firing_index(cusum, spike) <= 1
    assert first_firing_index(zscore, spike) <= 2


def test_zscore_detects_a_step_change():
    detector = ThreeSigmaDetector("catalogue", "latency_p95_ms", warmup=10, window=30)
    feed(detector, QUIET_LATENCY * 2)
    assert len(feed(detector, [7470.0, 7480.0], start=200)) == 1


@pytest.mark.parametrize("kind", ["ewma", "zscore", "cusum", "static"])
def test_every_detector_is_constructible_by_name(kind):
    detector = build_detector(kind, service="catalogue", metric="latency_p95_ms")
    assert detector.name == kind


def test_unknown_detector_name_is_rejected():
    with pytest.raises(ValueError, match="unknown detector"):
        build_detector("magic", service="catalogue", metric="latency_p95_ms")
