import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from deploy_window import DeployWindowTracker  # noqa: E402

BASE = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    return BASE + timedelta(seconds=seconds)


def test_no_deploy_means_the_ordinary_evidence_bar():
    tracker = DeployWindowTracker(window_seconds=120, breaches_in_window=4)
    assert tracker.required_breaches("catalogue", at(0), default=2) == (2, None)


def test_inside_a_deploy_window_the_bar_rises_and_the_deploy_is_named():
    tracker = DeployWindowTracker(window_seconds=120, breaches_in_window=4)
    tracker.record("dep-2026-09-13-0007", "catalogue", at(0))

    needed, deploy_id = tracker.required_breaches("catalogue", at(30), default=2)

    assert needed == 4
    assert deploy_id == "dep-2026-09-13-0007"


def test_a_deploy_window_never_fully_suppresses_detection():
    """The bad_deploy_latency fault is the one we most need to catch.

    Hard-muting a service during its deploy would silence exactly the
    incidents this system exists to diagnose, so the window may only ever
    raise the bar — never close the gate.
    """
    tracker = DeployWindowTracker(window_seconds=120, breaches_in_window=4)
    tracker.record("dep-2026-09-13-0007", "catalogue", at(0))

    needed, _ = tracker.required_breaches("catalogue", at(10), default=2)

    assert needed < float("inf")
    assert isinstance(needed, int)


def test_the_window_expires():
    tracker = DeployWindowTracker(window_seconds=120, breaches_in_window=4)
    tracker.record("dep-2026-09-13-0007", "catalogue", at(0))

    assert tracker.required_breaches("catalogue", at(121), default=2) == (2, None)


def test_a_deploy_only_affects_its_own_service():
    tracker = DeployWindowTracker(window_seconds=120, breaches_in_window=4)
    tracker.record("dep-2026-09-13-0007", "catalogue", at(0))

    assert tracker.required_breaches("payment", at(10), default=2) == (2, None)


def test_the_newest_deploy_wins_even_if_events_arrive_out_of_order():
    tracker = DeployWindowTracker(window_seconds=120, breaches_in_window=4)
    tracker.record("dep-newer", "catalogue", at(60))
    tracker.record("dep-older", "catalogue", at(0))

    _, deploy_id = tracker.required_breaches("catalogue", at(70), default=2)

    assert deploy_id == "dep-newer"


def test_a_higher_default_bar_is_never_lowered_by_a_deploy_window():
    tracker = DeployWindowTracker(window_seconds=120, breaches_in_window=4)
    tracker.record("dep-2026-09-13-0007", "catalogue", at(0))

    needed, _ = tracker.required_breaches("catalogue", at(10), default=6)

    assert needed == 6
