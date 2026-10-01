import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scenarios import SEVERITY_LEVELS, severity_suite  # noqa: E402
from sweep import (  # noqa: E402
    SeverityPoint,
    best_adaptive_rate,
    crossover,
    separation,
    verdict,
)


def point(cpu_limit, baseline, under_fault, **outcomes):
    return SeverityPoint(
        cpu_limit=cpu_limit,
        baseline_p95_ms=baseline,
        fault_p95_ms=under_fault,
        outcomes={k: v for k, v in outcomes.items()},
    )


# ------------------------------------------------------------- the suite

def test_severity_suite_varies_only_the_magnitude():
    gentle = severity_suite(0.05, services=("catalogue", "orders"))
    harsh = severity_suite(0.002, services=("catalogue", "orders"))
    assert [s.service for s in gentle] == [s.service for s in harsh]
    assert [s.fault_type for s in gentle] == ["bad_deploy_latency"] * 2
    assert [s.params["cpu_limit"] for s in gentle] == [0.05, 0.05]
    assert [s.params["cpu_limit"] for s in harsh] == [0.002, 0.002]


def test_severity_suite_rejects_a_service_the_injector_would_refuse():
    with pytest.raises(ValueError):
        severity_suite(0.01, services=("not-a-service",))


def test_default_levels_span_weak_to_strong():
    assert len(SEVERITY_LEVELS) >= 3
    assert list(SEVERITY_LEVELS) == sorted(SEVERITY_LEVELS, reverse=True), (
        "a higher cpu_limit is a gentler fault, so the levels run weakest first"
    )


# ------------------------------------------------------------- the curve

def test_impact_is_the_measured_rise_not_the_quota():
    p = point(0.002, 20.0, 260.0, ewma=(3, 3), static=(0, 3))
    assert p.impact_ms == 240.0


def test_a_rung_that_measured_nothing_is_not_on_the_curve():
    assert not point(0.05, None, None, ewma=(1, 1)).observed
    assert not point(0.05, 20.0, 25.0).observed       # no detector outcomes
    assert point(0.05, 20.0, 25.0, ewma=(1, 1)).observed


def test_best_adaptive_is_the_strongest_not_the_average():
    """The claim is that a learned baseline *can* do what a threshold cannot."""
    p = point(0.01, 20.0, 100.0, ewma=(3, 3), cusum=(1, 3), static=(0, 3))
    assert best_adaptive_rate(p) == 1.0


def test_crossover_is_the_smallest_impact_where_the_threshold_catches_up():
    points = [
        point(0.05, 20.0, 40.0, ewma=(3, 3), static=(0, 3)),    # 20 ms, gap
        point(0.01, 20.0, 220.0, ewma=(3, 3), static=(1, 3)),   # 200 ms, gap
        point(0.005, 20.0, 540.0, ewma=(3, 3), static=(3, 3)),  # 520 ms, caught up
        point(0.002, 20.0, 820.0, ewma=(3, 3), static=(3, 3)),  # 800 ms, also level
    ]
    found = crossover(points)
    assert found is not None and found.impact_ms == 520.0


def test_crossover_orders_by_impact_not_by_quota():
    """A busier run can make a gentler quota bite harder; impact is the x-axis."""
    points = [
        point(0.002, 20.0, 120.0, ewma=(3, 3), static=(3, 3)),  # 100 ms, caught up
        point(0.05, 20.0, 520.0, ewma=(3, 3), static=(3, 3)),   # 500 ms, also level
    ]
    found = crossover(points)
    assert found.cpu_limit == 0.002, "the lower-impact rung is the crossover"


def test_no_crossover_when_the_threshold_never_catches_up():
    points = [
        point(0.05, 20.0, 40.0, ewma=(3, 3), static=(0, 3)),
        point(0.01, 20.0, 220.0, ewma=(3, 3), static=(1, 3)),
    ]
    assert crossover(points) is None


def test_separation_puts_the_most_discriminating_fault_first():
    points = [
        point(0.05, 20.0, 40.0, ewma=(1, 3), static=(0, 3)),    # gap 0.33
        point(0.01, 20.0, 220.0, ewma=(3, 3), static=(0, 3)),   # gap 1.00
    ]
    assert separation(points)[0][0].cpu_limit == 0.01


# ------------------------------------------------------------- the sentence

def test_verdict_refuses_to_draw_a_curve_from_one_rung():
    assert "Not enough rungs" in verdict([point(0.05, 20.0, 40.0, ewma=(3, 3))])


def test_verdict_reports_a_crossover_when_there_is_one():
    points = [
        point(0.05, 20.0, 40.0, ewma=(3, 3), static=(0, 3)),
        point(0.002, 20.0, 520.0, ewma=(3, 3), static=(3, 3)),
    ]
    text = verdict(points)
    assert "caught up by 500 ms" in text
    assert "earns its place on subtle regressions" in text


def test_verdict_says_above_the_range_rather_than_absent():
    """Not finding a crossover is a statement about the range, not the detectors."""
    points = [
        point(0.05, 20.0, 40.0, ewma=(3, 3), static=(0, 3)),
        point(0.01, 20.0, 220.0, ewma=(3, 3), static=(1, 3)),
    ]
    text = verdict(points)
    assert "above the largest fault measured, not absent" in text


def test_verdict_does_not_claim_a_benefit_that_was_not_measured():
    points = [
        point(0.05, 20.0, 40.0, ewma=(3, 3), static=(3, 3)),
        point(0.002, 20.0, 520.0, ewma=(3, 3), static=(3, 3)),
    ]
    assert "no benefit from a learned baseline" in verdict(points)
