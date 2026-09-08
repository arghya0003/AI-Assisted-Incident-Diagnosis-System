import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from main import (
    EWMAAnomalyDetector,
    StaticThresholdDetector,
    group_detections,
    is_within_deploy_settle_window,
)


def test_detects_large_step_change():
    detector = EWMAAnomalyDetector(alpha=0.35, z_threshold=3.0, warmup=5)

    for value in [10.0, 10.2, 9.8, 10.1, 10.3, 10.0, 10.1, 9.9, 10.2, 10.1]:
        detector.update(value)

    anomaly = detector.update(42.0)

    assert anomaly is not None
    assert anomaly["severity"] in {"medium", "high"}
    assert anomaly["service"] == "catalogue"
    assert anomaly["metric"] == "latency_p99_ms"


def test_ewma_ignores_stable_noise():
    detector = EWMAAnomalyDetector(alpha=0.2, z_threshold=3.0, warmup=5)

    for value in [10.0, 10.1, 9.9, 10.2, 9.8, 10.0, 10.1, 9.9, 10.0, 10.1, 9.9, 10.0]:
        assert detector.update(value) is None


def test_static_threshold_freezes_after_warmup():
    detector = StaticThresholdDetector(warmup=5, k=3.0)

    for value in [10.0, 10.1, 9.9, 10.0, 10.0]:
        assert detector.update(value) is None

    threshold_after_warmup = detector._threshold
    assert threshold_after_warmup is not None

    # a sustained shift that a live-adapting detector would eventually treat
    # as the new normal never moves this detector's frozen threshold
    for value in [30.0, 30.0, 30.0, 30.0]:
        detector.update(value)
    assert detector._threshold == threshold_after_warmup


def test_static_threshold_fires_on_spike():
    detector = StaticThresholdDetector(warmup=5, k=3.0)
    for value in [10.0, 10.1, 9.9, 10.0, 10.0]:
        detector.update(value)

    anomaly = detector.update(500.0)
    assert anomaly is not None
    assert anomaly["severity"] == "high"


def test_group_detections_dedupes_services_and_metrics():
    raw = [
        {"service": "catalogue", "metric": "latency_p99_ms", "severity": "high",
         "t_onset": "2026-08-12T20:44:50.000Z"},
        {"service": "catalogue", "metric": "error_rate", "severity": "medium",
         "t_onset": "2026-08-12T20:44:55.000Z"},
        {"service": "front-end", "metric": "latency_p99_ms", "severity": "medium",
         "t_onset": "2026-08-12T20:44:52.000Z"},
    ]

    grouped = group_detections(raw, id_prefix="ewma")

    assert grouped["services"] == ["catalogue", "front-end"]
    assert grouped["metrics"] == ["error_rate", "latency_p99_ms"]
    assert grouped["severity"] == "high"  # highest severity in the group wins
    assert grouped["t_onset"] == "2026-08-12T20:44:50.000Z"  # earliest onset
    assert grouped["anomaly_id"].startswith("anom-ewma-")


def test_deploy_settle_window_suppresses_only_briefly_after_deploy():
    assert is_within_deploy_settle_window(last_deploy_ts=None, now=1000.0) is False
    assert is_within_deploy_settle_window(last_deploy_ts=1000.0, now=1003.0) is True
    assert is_within_deploy_settle_window(last_deploy_ts=1000.0, now=1030.0) is False
