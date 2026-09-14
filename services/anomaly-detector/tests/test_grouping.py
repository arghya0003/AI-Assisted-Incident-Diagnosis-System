import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from detectors import Signal  # noqa: E402
from grouping import AnomalyGrouper, format_ts, parse_ts  # noqa: E402

BASE = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    return BASE + timedelta(seconds=seconds)


def signal(service, metric, seconds, severity="high", onset=None, **kwargs):
    return Signal(
        service=service,
        metric=metric,
        value=7470.0,
        baseline=36.0,
        score=4.2,
        severity=severity,
        timestamp=format_ts(at(seconds)),
        detector="ewma",
        onset_timestamp=format_ts(at(onset)) if onset is not None else None,
        **kwargs,
    )


def test_one_fault_across_many_services_becomes_a_single_event():
    """The alert-storm requirement: fifteen breaches, one incident."""
    grouper = AnomalyGrouper(group_delay_seconds=15)

    for i, (service, metric) in enumerate([
        ("catalogue", "latency_p50_ms"),
        ("catalogue", "latency_p95_ms"),
        ("catalogue", "latency_p99_ms"),
        ("catalogue", "error_rate"),
        ("front-end", "latency_p95_ms"),
        ("front-end", "error_rate"),
    ]):
        grouper.add(signal(service, metric, seconds=i))

    events = grouper.flush(at(20))

    assert len(events) == 1
    event = events[0]
    assert event["services"] == ["catalogue", "front-end"]
    assert event["metrics"] == [
        "error_rate", "latency_p50_ms", "latency_p95_ms", "latency_p99_ms",
    ]
    assert len(event["contributors"]) == 6


def test_nothing_is_emitted_before_the_grouping_delay_elapses():
    grouper = AnomalyGrouper(group_delay_seconds=15)
    grouper.add(signal("catalogue", "latency_p95_ms", seconds=0))

    assert grouper.flush(at(10)) == []
    assert len(grouper.flush(at(16))) == 1


def test_detection_time_comes_from_the_first_signal_not_the_emit_time():
    """Grouping delay must not inflate the measured detection latency."""
    grouper = AnomalyGrouper(group_delay_seconds=15)
    grouper.add(signal("catalogue", "latency_p95_ms", seconds=0, onset=-10))
    grouper.add(signal("front-end", "latency_p95_ms", seconds=12))

    event = grouper.flush(at(30))[0]

    assert event["t_detected"] == format_ts(at(0))
    assert event["t_onset"] == format_ts(at(-10))


def test_repeated_breaches_of_one_metric_do_not_multiply_members():
    grouper = AnomalyGrouper(group_delay_seconds=15)
    for second in range(0, 12, 2):
        grouper.add(signal("catalogue", "latency_p95_ms", seconds=second))

    event = grouper.flush(at(20))[0]

    assert event["services"] == ["catalogue"]
    assert event["metrics"] == ["latency_p95_ms"]


def test_an_ongoing_fault_does_not_re_alert_during_cooldown():
    grouper = AnomalyGrouper(group_delay_seconds=15, cooldown_seconds=120)
    grouper.add(signal("catalogue", "latency_p95_ms", seconds=0))
    assert len(grouper.flush(at(16))) == 1

    for second in range(20, 120, 5):
        grouper.add(signal("catalogue", "latency_p95_ms", seconds=second))
        assert grouper.flush(at(second)) == []


def test_a_new_fault_after_cooldown_expires_alerts_again():
    grouper = AnomalyGrouper(group_delay_seconds=15, cooldown_seconds=60)
    grouper.add(signal("catalogue", "latency_p95_ms", seconds=0))
    grouper.flush(at(16))

    grouper.add(signal("catalogue", "latency_p95_ms", seconds=300))
    assert len(grouper.flush(at(320))) == 1


def scored(service, metric, seconds, score):
    s = signal(service, metric, seconds)
    s.score = score
    return s


def test_a_much_larger_spike_breaks_through_an_earlier_cooldown():
    """Regression: a real fault was swallowed by a false alarm's cooldown.

    On 2026-09-14 a startup blip (score ~2.6) muted catalogue, every further
    blip sample extended the mute, and a genuine 362 ms regression (score
    ~18.6) two minutes later was treated as the same incident.
    """
    grouper = AnomalyGrouper(group_delay_seconds=15, cooldown_seconds=120)
    for second in (0, 5, 10):
        grouper.add(scored("catalogue", "latency_p99_ms", second, 2.58))
    assert len(grouper.flush(at(16))) == 1

    for second in (60, 90, 120):
        grouper.add(scored("catalogue", "latency_p99_ms", second, 2.58))
        assert grouper.flush(at(second)) == []

    grouper.add(scored("catalogue", "latency_p95_ms", 150, 18.62))
    events = grouper.flush(at(170))

    assert len(events) == 1
    assert events[0]["services"] == ["catalogue"]

    # The escalated incident sets the new bar, so the same fault carrying on
    # does not re-alert every cycle.
    grouper.add(scored("catalogue", "latency_p95_ms", 180, 19.9))
    assert grouper.flush(at(200)) == []


def test_a_modestly_worse_reading_stays_part_of_the_ongoing_incident():
    grouper = AnomalyGrouper(group_delay_seconds=15, cooldown_seconds=120)
    grouper.add(scored("catalogue", "latency_p95_ms", 0, 4.0))
    grouper.flush(at(16))

    grouper.add(scored("catalogue", "latency_p95_ms", 30, 8.0))

    assert grouper.flush(at(60)) == []


def test_an_unrelated_service_still_alerts_while_another_is_cooling():
    grouper = AnomalyGrouper(group_delay_seconds=15, cooldown_seconds=120)
    grouper.add(signal("catalogue", "latency_p95_ms", seconds=0))
    grouper.flush(at(16))

    grouper.add(signal("payment", "error_rate", seconds=30))
    events = grouper.flush(at(50))

    assert len(events) == 1
    assert events[0]["services"] == ["payment"]


def test_event_severity_is_the_worst_of_its_members():
    grouper = AnomalyGrouper(group_delay_seconds=15)
    grouper.add(signal("catalogue", "latency_p50_ms", seconds=0, severity="low"))
    grouper.add(signal("catalogue", "latency_p99_ms", seconds=1, severity="high"))
    grouper.add(signal("front-end", "error_rate", seconds=2, severity="medium"))

    assert grouper.flush(at(20))[0]["severity"] == "high"


def test_deploy_context_is_carried_onto_the_event():
    grouper = AnomalyGrouper(group_delay_seconds=15)
    grouper.add(signal("catalogue", "latency_p95_ms", seconds=0,
                        in_deploy_window=True, deploy_id="dep-2026-09-13-0007"))

    event = grouper.flush(at(20))[0]

    assert event["in_deploy_window"] is True
    assert event["related_deploy_ids"] == ["dep-2026-09-13-0007"]


def test_event_satisfies_the_frozen_contract_shape():
    grouper = AnomalyGrouper(group_delay_seconds=15)
    grouper.add(signal("catalogue", "latency_p95_ms", seconds=0))
    event = grouper.flush(at(20))[0]

    for field in ("anomaly_id", "services", "metrics", "severity",
                   "t_detected", "t_onset", "evidence_window"):
        assert field in event, f"{field} missing from anomalies.detected payload"

    assert set(event["evidence_window"]) == {"start", "end"}
    assert parse_ts(event["evidence_window"]["start"]) <= parse_ts(event["evidence_window"]["end"])
    assert event["anomaly_id"].startswith("anom-")


def test_anomaly_ids_are_unique_across_events():
    grouper = AnomalyGrouper(group_delay_seconds=15, cooldown_seconds=10)
    ids = []
    for start in (0, 100, 200):
        grouper.add(signal("catalogue", "latency_p95_ms", seconds=start))
        ids.append(grouper.flush(at(start + 20))[0]["anomaly_id"])

    assert len(set(ids)) == 3
