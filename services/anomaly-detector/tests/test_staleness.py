import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from staleness import StalenessMonitor  # noqa: E402

BASE = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    return BASE + timedelta(seconds=seconds)


def reporting(monitor, service, from_s, to_s, step=5):
    for second in range(from_s, to_s, step):
        monitor.observe(service, at(second))


def test_a_reporting_service_is_not_stale():
    monitor = StalenessMonitor(stale_after_seconds=30)
    reporting(monitor, "payment", 0, 60)

    assert monitor.check(at(60)) == []


def test_a_service_that_stops_reporting_is_flagged():
    """The regression test for the miss the first live run exposed.

    A crashed container disappears from Prometheus, so it stops producing
    samples entirely rather than producing bad ones. Every per-sample
    detector is blind to that; silence has to be its own signal.
    """
    monitor = StalenessMonitor(stale_after_seconds=30)
    reporting(monitor, "payment", 0, 60)

    signals = monitor.check(at(95))

    assert len(signals) == 1
    assert signals[0].service == "payment"
    assert signals[0].metric == "liveness"
    assert signals[0].severity == "high"


def test_the_outage_is_dated_from_when_data_stopped_not_when_it_was_noticed():
    monitor = StalenessMonitor(stale_after_seconds=30)
    reporting(monitor, "payment", 0, 60)

    signal = monitor.check(at(95))[0]

    # Last sample was at t=55; the timeout only expired later.
    assert signal.onset_timestamp.startswith("2026-09-13T12:00:55")


def test_a_service_is_not_flagged_twice_for_one_outage():
    monitor = StalenessMonitor(stale_after_seconds=30)
    reporting(monitor, "payment", 0, 60)

    assert len(monitor.check(at(95))) == 1
    assert monitor.check(at(120)) == []
    assert monitor.check(at(300)) == []


def test_a_recovered_service_can_be_flagged_again_later():
    monitor = StalenessMonitor(stale_after_seconds=30)
    reporting(monitor, "payment", 0, 60)
    assert len(monitor.check(at(95))) == 1

    reporting(monitor, "payment", 100, 160)
    assert monitor.check(at(160)) == []

    assert len(monitor.check(at(200))) == 1


def test_a_barely_seen_service_is_not_flagged():
    """Probably mid-startup, not mid-outage."""
    monitor = StalenessMonitor(stale_after_seconds=30, min_observations=3)
    monitor.observe("ghost", at(0))

    assert monitor.check(at(500)) == []


def test_only_the_silent_service_is_flagged():
    monitor = StalenessMonitor(stale_after_seconds=30)
    reporting(monitor, "payment", 0, 60)
    reporting(monitor, "catalogue", 0, 95)

    signals = monitor.check(at(95))

    assert [s.service for s in signals] == ["payment"]


def test_the_gap_is_reported_as_the_signal_value():
    monitor = StalenessMonitor(stale_after_seconds=30)
    reporting(monitor, "payment", 0, 60)

    signal = monitor.check(at(115))[0]

    assert signal.value == 60.0  # last sample t=55, checked at t=115
    assert signal.score == 2.0   # twice the staleness threshold
