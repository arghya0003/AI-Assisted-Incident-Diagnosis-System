import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from main import EWMAAnomalyDetector


def test_detects_large_step_change():
    detector = EWMAAnomalyDetector(alpha=0.35, z_threshold=3.0, warmup=5)

    for value in [10.0, 10.2, 9.8, 10.1, 10.3, 10.0, 10.1, 9.9, 10.2, 10.1]:
        detector.update(value)

    anomaly = detector.update(42.0)

    assert anomaly is not None
    assert anomaly["severity"] in {"medium", "high"}
    assert anomaly["service"] == "catalogue"
    assert anomaly["metric"] == "latency_p99_ms"
