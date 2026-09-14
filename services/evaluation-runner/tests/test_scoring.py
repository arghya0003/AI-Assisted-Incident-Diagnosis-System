import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scoring import (  # noqa: E402
    DetectedEvent,
    Outcome,
    Scenario,
    false_positives_per_hour,
    find_false_positives,
    fault_window,
    mean_reciprocal_rank,
    percentile,
    quiet_seconds,
    reciprocal_rank,
    score_scenario,
    summarize,
    top_k_accuracy,
)

BASE = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    return BASE + timedelta(seconds=seconds)


def scenario(service="catalogue", inject=0, recovered=60, fault_type="bad_deploy_latency",
             scenario_id="scn-1"):
    return Scenario(
        scenario_id=scenario_id,
        fault_type=fault_type,
        ground_truth_service=service,
        t_inject=at(inject),
        t_recovered=at(recovered) if recovered is not None else None,
    )


def event(services=("catalogue",), detected=20, anomaly_id="anom-1"):
    return DetectedEvent(
        anomaly_id=anomaly_id,
        services=tuple(services),
        metrics=("latency_p95_ms",),
        severity="high",
        t_detected=at(detected),
        t_onset=at(detected - 5),
    )


def test_a_correct_alert_inside_the_window_is_a_detection():
    result = score_scenario(scenario(), [event(detected=20)])

    assert result.outcome is Outcome.DETECTED
    assert result.detection_latency_s == 20.0
    assert result.matched_event_id == "anom-1"


def test_no_alert_at_all_is_a_miss():
    result = score_scenario(scenario(), [])

    assert result.outcome is Outcome.MISSED
    assert result.detection_latency_s is None


def test_an_alert_naming_only_the_wrong_service_is_misattributed_not_detected():
    """Noticing the wrong service is a different failure from noticing nothing."""
    result = score_scenario(scenario(service="catalogue"), [event(services=("payment",))])

    assert result.outcome is Outcome.MISATTRIBUTED
    assert result.detection_latency_s is None


def test_a_grouped_alert_counts_when_the_culprit_is_among_its_members():
    result = score_scenario(
        scenario(service="catalogue"),
        [event(services=("front-end", "catalogue"))],
    )

    assert result.outcome is Outcome.DETECTED


def test_latency_is_measured_to_the_first_correct_alert():
    events = [
        event(services=("payment",), detected=10, anomaly_id="wrong"),
        event(services=("catalogue",), detected=25, anomaly_id="right"),
        event(services=("catalogue",), detected=40, anomaly_id="later"),
    ]

    result = score_scenario(scenario(), events)

    assert result.matched_event_id == "right"
    assert result.detection_latency_s == 25.0


def test_an_alert_before_injection_does_not_count_as_detecting_it():
    result = score_scenario(scenario(inject=100, recovered=160), [event(detected=10)])

    assert result.outcome is Outcome.MISSED


def test_the_window_extends_past_recovery_because_rate_windows_decay():
    """A 1m rate window means effects outlast the fault; that isn't a miss."""
    result = score_scenario(scenario(inject=0, recovered=60), [event(detected=100)])

    assert result.outcome is Outcome.DETECTED


def test_an_alert_long_after_recovery_is_not_credited():
    result = score_scenario(scenario(inject=0, recovered=60), [event(detected=1000)])

    assert result.outcome is Outcome.MISSED


def test_a_scenario_that_never_recovered_still_has_a_window():
    start, end = fault_window(scenario(inject=0, recovered=None))

    assert start == at(0)
    assert end > at(0)


def test_a_fault_on_a_service_with_no_telemetry_is_unobservable_not_missed():
    """Scoring the testbed's gaps as detector failures would be dishonest."""
    result = score_scenario(scenario(), [], observable=False)

    assert result.outcome is Outcome.UNOBSERVABLE


def test_an_unobservable_scenario_that_was_still_detected_counts_as_detected():
    """Being hard to see is not a reason to discount a real success."""
    result = score_scenario(scenario(), [event(detected=20)], observable=False)

    assert result.outcome is Outcome.DETECTED
    assert result.detection_latency_s == 20.0


def test_unobservable_scenarios_still_count_against_the_headline_rate():
    """The overall rate must stay the number that cannot be gamed."""
    results = [
        score_scenario(scenario(scenario_id="a"), [event(detected=10)]),
        score_scenario(scenario(scenario_id="b"), [], observable=False),
    ]

    summary = summarize(results)["overall"]

    assert summary.total == 2
    assert summary.unobservable == 1
    assert summary.detection_rate == pytest.approx(0.5)
    assert summary.detection_rate_observable == pytest.approx(1.0)


def test_observable_rate_is_none_when_nothing_was_observable():
    results = [score_scenario(scenario(), [], observable=False)]

    assert summarize(results)["overall"].detection_rate_observable is None


def test_alerts_outside_every_fault_window_are_false_positives():
    scenarios = [scenario(inject=0, recovered=60)]
    events = [
        event(detected=30, anomaly_id="during"),
        event(detected=5000, anomaly_id="quiet-time"),
    ]

    false_positives = find_false_positives(events, scenarios)

    assert [e.anomaly_id for e in false_positives] == ["quiet-time"]


def test_quiet_time_excludes_fault_windows():
    scenarios = [scenario(inject=100, recovered=200)]

    quiet = quiet_seconds(at(0), at(1000), scenarios)

    # 1000s observed, minus a 100->200 fault plus the 90s decay grace.
    assert quiet == pytest.approx(1000 - 190)


def test_overlapping_fault_windows_are_not_double_subtracted():
    """Otherwise the denominator shrinks and the FP rate looks worse than it is."""
    scenarios = [
        scenario(inject=100, recovered=200, scenario_id="a"),
        scenario(inject=150, recovered=250, scenario_id="b"),
    ]

    quiet = quiet_seconds(at(0), at(1000), scenarios)

    # Union of the two windows is 100 -> 250+90, i.e. 240s of busy time.
    assert quiet == pytest.approx(1000 - 240)


def test_false_positive_rate_is_per_hour():
    assert false_positives_per_hour(2, quiet_s=7200) == pytest.approx(1.0)


def test_false_positive_rate_is_undefined_without_quiet_observation():
    assert false_positives_per_hour(0, quiet_s=0) is None


def test_summary_splits_by_fault_type_and_totals_overall():
    results = [
        score_scenario(scenario(fault_type="bad_deploy_latency", scenario_id="a"),
                        [event(detected=10)]),
        score_scenario(scenario(fault_type="bad_deploy_latency", scenario_id="b"),
                        [event(detected=30)]),
        score_scenario(scenario(fault_type="service_crash", scenario_id="c"), []),
    ]

    summaries = summarize(results)

    assert summaries["bad_deploy_latency"].detected == 2
    assert summaries["bad_deploy_latency"].median_latency_s == pytest.approx(20.0)
    assert summaries["service_crash"].missed == 1
    assert summaries["overall"].total == 3
    assert summaries["overall"].detection_rate == pytest.approx(2 / 3)


def test_percentile_of_nothing_is_none():
    assert percentile([], 95) is None


def test_percentile_picks_a_real_observed_value():
    assert percentile([1.0, 2.0, 3.0, 4.0], 50) in {2.0, 3.0}
    assert percentile([1.0, 2.0, 3.0, 4.0], 100) == 4.0


def test_reciprocal_rank_rewards_the_top_position():
    assert reciprocal_rank(["catalogue", "payment"], "catalogue") == 1.0
    assert reciprocal_rank(["payment", "catalogue"], "catalogue") == pytest.approx(0.5)
    assert reciprocal_rank(["payment", "user"], "catalogue") == 0.0


def test_mrr_of_an_unrun_comparison_is_none_not_zero():
    """Reporting 0.0 would look like a measured failure rather than no data."""
    assert mean_reciprocal_rank([]) is None


def test_mrr_averages_reciprocal_ranks():
    rankings = [(["catalogue"], "catalogue"), (["payment", "catalogue"], "catalogue")]
    assert mean_reciprocal_rank(rankings) == pytest.approx(0.75)


def test_top_k_accuracy_counts_hits_within_k():
    rankings = [
        (["catalogue", "payment", "user"], "catalogue"),
        (["payment", "user", "catalogue"], "catalogue"),
        (["payment", "user", "orders"], "catalogue"),
    ]

    assert top_k_accuracy(rankings, k=1) == pytest.approx(1 / 3)
    assert top_k_accuracy(rankings, k=3) == pytest.approx(2 / 3)
