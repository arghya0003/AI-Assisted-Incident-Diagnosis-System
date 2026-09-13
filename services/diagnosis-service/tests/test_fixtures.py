"""Phase 2: the hand-written anomaly fixtures load, are realistic, and cover the scenarios
later phases are developed against."""

from datetime import timedelta

import pytest

from app.fixtures import FIXTURE_ID_PREFIX, load_fixtures
from app.graph import load_graph

FIXTURES = load_fixtures()
GRAPH = load_graph()
# Metrics metrics-bridge publishes (services/metrics-bridge/main.py, METRIC_QUERIES).
METRIC_NAMES = {
    "request_rate", "error_rate", "latency_p50_ms", "latency_p95_ms", "latency_p99_ms",
    "cpu_rate", "memory_bytes",
}
# Services fault-injector accepts (services/fault-injector/main.py, KNOWN_SERVICES).
INJECTABLE_SERVICES = {"front-end", "catalogue", "payment", "user", "carts", "orders", "shipping"}


def _ids(fixtures):
    return [f.event.anomaly_id for f in fixtures]


def test_eight_to_ten_fixtures():
    assert 8 <= len(FIXTURES) <= 10


def test_ids_are_unique_prefixed_and_match_filenames():
    ids = _ids(FIXTURES)
    assert len(set(ids)) == len(ids)
    for fixture in FIXTURES:
        assert fixture.event.anomaly_id.startswith(FIXTURE_ID_PREFIX)
        assert fixture.path.stem == fixture.event.anomaly_id


@pytest.mark.parametrize("fixture", FIXTURES, ids=_ids(FIXTURES))
def test_fixture_uses_real_names(fixture):
    assert set(fixture.event.services) <= GRAPH.nodes
    assert set(fixture.event.metrics) <= METRIC_NAMES
    assert fixture.event.severity in {"high", "medium"}  # what the detector emits today
    if fixture.meta.ground_truth_service is not None:
        assert fixture.meta.ground_truth_service in INJECTABLE_SERVICES


@pytest.mark.parametrize("fixture", FIXTURES, ids=_ids(FIXTURES))
def test_fixture_times_are_consistent(fixture):
    event = fixture.event
    for moment in (event.t_onset, event.t_detected, event.evidence_window.start):
        assert moment.tzinfo is not None
    assert event.t_onset <= event.t_detected
    assert event.evidence_window.start <= event.t_onset <= event.evidence_window.end


@pytest.mark.parametrize(
    "fixture", [f for f in FIXTURES if f.meta.shape == "m2_current"],
    ids=_ids(f for f in FIXTURES if f.meta.shape == "m2_current"),
)
def test_m2_current_shape_matches_the_detector_today(fixture):
    event = fixture.event
    assert len(event.services) == 1 and len(event.metrics) == 1
    # Zero-width window at onset (issue #3).
    assert event.evidence_window.start == event.evidence_window.end == event.t_onset


def test_raw_event_has_no_fixture_metadata():
    for fixture in FIXTURES:
        assert "_fixture" not in fixture.raw


def test_covers_the_scenarios_later_phases_need():
    fault_types = {f.meta.fault_type for f in FIXTURES}
    assert {"bad_deploy_latency", "service_crash", "db_pool_saturation"} <= fault_types
    assert None in fault_types, "needs a benign or ambiguous case"
    assert {"m2_current", "contract"} <= {f.meta.shape for f in FIXTURES}
    assert any(len(f.event.services) >= 3 for f in FIXTURES), "needs a multi-service cascade"
    assert any(
        f.meta.ground_truth_service is not None
        and f.meta.ground_truth_service not in f.event.services
        for f in FIXTURES
    ), "needs a case where the root cause is absent from the anomaly's services"


def test_context_ids_are_prefixed_and_unique():
    related_ids = [r.anomaly_id for f in FIXTURES for r in f.meta.context.related_anomalies]
    anomaly_ids = _ids(FIXTURES) + related_ids
    assert len(set(anomaly_ids)) == len(anomaly_ids)
    deploy_ids = [d.deploy_id for f in FIXTURES for d in f.meta.context.deploys]
    assert len(set(deploy_ids)) == len(deploy_ids)


@pytest.mark.parametrize("fixture", FIXTURES, ids=_ids(FIXTURES))
def test_fixture_context_is_realistic(fixture):
    onset = fixture.event.t_onset
    assert fixture.meta.context.deploys, "deploy-emitter deploys every two minutes; a real window is never empty"
    for deploy in fixture.meta.context.deploys:
        assert deploy.service in INJECTABLE_SERVICES
        assert timedelta(0) < onset - deploy.time <= timedelta(minutes=30)
    for other in fixture.meta.context.related_anomalies:
        assert set(other.services) <= GRAPH.nodes and set(other.metrics) <= METRIC_NAMES
        assert abs(other.t_onset - onset) <= timedelta(seconds=120)


@pytest.mark.parametrize("fixture", FIXTURES, ids=_ids(FIXTURES))
def test_only_deploy_faults_have_a_deploy_to_the_root_cause(fixture):
    truth = fixture.meta.ground_truth_service
    if truth is None:
        return
    to_truth = [d for d in fixture.meta.context.deploys if d.service == truth]
    if fixture.meta.fault_type == "bad_deploy_latency":
        # The injector records the deploy seconds before it throttles the service.
        assert any(fixture.event.t_onset - d.time <= timedelta(seconds=60) for d in to_truth)
    else:
        assert to_truth == [], "a crash or saturation scenario must not hand scoring a deploy to the cause"


def test_same_symptom_different_cause_pair_really_matches():
    by_id = {f.event.anomaly_id: f for f in FIXTURES}
    a, b = by_id["anom-fx-04"], by_id["anom-fx-06"]
    assert (a.event.services, a.event.metrics) == (b.event.services, b.event.metrics)
    assert a.meta.ground_truth_service != b.meta.ground_truth_service
