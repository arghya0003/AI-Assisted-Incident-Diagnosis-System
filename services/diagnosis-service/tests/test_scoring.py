"""Phase 4: deterministic candidate scoring. No database, no network, no LLM."""

import dataclasses
import math
import socket
from datetime import datetime, timedelta, timezone

import pytest

from app.fixtures import load_fixtures
from app.graph import load_graph
from app.models import AnomalyEvent, Deploy, Signals, SimilarIncident
from app.scoring import ScoringConfig, ScoringInputs, score_candidates
from app.settings import settings

GRAPH = load_graph()
CONFIG = ScoringConfig.from_settings(settings)
ONSET = datetime(2026, 9, 14, 10, 0, 0, tzinfo=timezone.utc)
FIXTURES = {f.event.anomaly_id: f for f in load_fixtures()}


def make_anomaly(services, anomaly_id="anom-t-1", onset=ONSET):
    return AnomalyEvent(
        anomaly_id=anomaly_id,
        services=list(services),
        metrics=["latency_p95_ms"],
        severity="high",
        t_detected=onset,
        t_onset=onset,
        evidence_window={"start": onset, "end": onset},
    )


def make_deploy(service, minutes_before, deploy_id=None):
    return Deploy(
        deploy_id=deploy_id or f"dep-t-{service}-{minutes_before}",
        service=service,
        version="1.0.0",
        commit_sha="abc1234",
        time=ONSET - timedelta(minutes=minutes_before),
    )


def score(services, deploys=(), related=(), config=CONFIG):
    inputs = ScoringInputs(anomaly=make_anomaly(services), related=list(related), deploys=list(deploys))
    return score_candidates(inputs, GRAPH, config)


def by_service(report):
    return {candidate.service: candidate for candidate in report.candidates}


def score_fixture(anomaly_id, extra_deploys=()):
    inputs = FIXTURES[anomaly_id].scoring_inputs()
    inputs = dataclasses.replace(inputs, deploys=inputs.deploys + list(extra_deploys))
    return score_candidates(inputs, GRAPH, CONFIG)


# ------------------------------------------------------------------ config


def test_default_weights_are_the_plan_values():
    assert CONFIG.weights == {
        "deploy_proximity": 0.40,
        "graph_proximity": 0.25,
        "co_anomaly": 0.20,
        "incident_similarity": 0.15,
    }
    assert set(CONFIG.weights) == set(Signals.model_fields)


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"weight_deploy": 0.50}, "sum to 1"),
        ({"weight_deploy": 0.75, "weight_graph": -0.10}, ">= 0"),
        ({"deploy_decay_minutes": 0}, "deploy_decay_minutes"),
        ({"co_anomaly_window_seconds": -1}, "co_anomaly_window_seconds"),
    ],
)
def test_invalid_config_is_rejected(changes, message):
    with pytest.raises(ValueError, match=message):
        dataclasses.replace(CONFIG, **changes)


def test_without_graph_zeroes_the_graph_weight_and_rescales_the_rest():
    no_graph = CONFIG.without_graph()
    assert no_graph.weights["graph_proximity"] == 0.0
    assert sum(no_graph.weights.values()) == pytest.approx(1.0)
    assert no_graph.weights["deploy_proximity"] / no_graph.weights["co_anomaly"] == pytest.approx(0.40 / 0.20)
    report = score(["front-end"], config=no_graph)
    # The signal is still computed and shown; it just no longer counts.
    assert report.weights["graph_proximity"] == 0.0
    assert all(c.signals.graph_proximity > 0 for c in report.candidates)
    for candidate in report.candidates:
        expected = sum(w * getattr(candidate.signals, name) for name, w in no_graph.weights.items())
        assert candidate.score == pytest.approx(expected)


# ------------------------------------------------------------------ candidates


def test_candidates_are_the_anomalous_services_plus_downstream():
    assert set(by_service(score(["front-end"]))) == {"front-end"} | set(GRAPH.downstream("front-end"))
    assert set(by_service(score(["payment"]))) == {"payment"}


def test_unknown_service_is_scored_without_crashing():
    (candidate,) = score(["checkout"]).candidates
    assert (candidate.service, candidate.kind, candidate.distance) == ("checkout", "unknown", 0)


# ------------------------------------------------------------------ deploy proximity


@pytest.mark.parametrize("minutes, approx_score", [(0, 1.0), (3, 0.74), (25, 0.08), (30, 0.05)])
def test_deploy_proximity_decays_exponentially(minutes, approx_score):
    candidate = by_service(score(["catalogue"], [make_deploy("catalogue", minutes)]))["catalogue"]
    assert candidate.signals.deploy_proximity == pytest.approx(math.exp(-minutes / 10))
    assert candidate.signals.deploy_proximity == pytest.approx(approx_score, abs=0.005)


@pytest.mark.parametrize("minutes", [30.5, -0.5], ids=["before-lookback", "after-onset"])
def test_deploys_outside_the_window_do_not_count(minutes):
    candidate = by_service(score(["catalogue"], [make_deploy("catalogue", minutes)]))["catalogue"]
    assert candidate.signals.deploy_proximity == 0.0
    assert candidate.deploy_id is None


def test_the_most_recent_deploy_is_used():
    deploys = [make_deploy("catalogue", 20, "dep-old"), make_deploy("catalogue", 4, "dep-new")]
    assert by_service(score(["catalogue"], deploys))["catalogue"].deploy_id == "dep-new"


def test_a_deploy_to_another_service_does_not_count():
    assert by_service(score(["catalogue"], [make_deploy("orders", 1)]))["catalogue"].deploy_id is None


# ------------------------------------------------------------------ graph proximity


def test_graph_proximity_by_hops():
    report = by_service(score(["front-end"]))
    expected = {"front-end": (0, 1.0), "catalogue": (1, 0.5), "catalogue-db": (2, 1 / 3), "rabbitmq": (3, 0.25)}
    for service, (hops, proximity) in expected.items():
        assert report[service].distance == hops
        assert report[service].signals.graph_proximity == pytest.approx(proximity)


def test_distance_is_from_the_nearest_anomalous_service():
    # shipping is 2 hops below front-end but 1 below orders.
    assert by_service(score(["front-end", "orders"]))["shipping"].distance == 1


# ------------------------------------------------------------------ co-anomaly


def test_the_deepest_anomalous_service_is_co_anomalous():
    report = by_service(score(["orders", "front-end"]))
    assert report["orders"].signals.co_anomaly == 1.0  # nothing it calls is anomalous
    assert report["front-end"].signals.co_anomaly == 0.0  # it calls orders, which is anomalous
    assert report["user"].signals.co_anomaly == 0.0  # not anomalous at all


def test_a_related_anomaly_marks_a_downstream_service():
    related = make_anomaly(["catalogue"], "anom-t-2", ONSET + timedelta(seconds=60))
    report = score(["front-end"], related=[related])
    candidates = by_service(report)
    assert report.related_anomaly_ids == ["anom-t-2"]
    assert report.anomalous_services == ["catalogue", "front-end"]
    assert candidates["catalogue"].signals.co_anomaly == 1.0
    assert candidates["front-end"].signals.co_anomaly == 0.0
    assert "ev:anom-t-1:anomaly:anom-t-2" in candidates["catalogue"].evidence_ids


def test_related_anomalies_outside_the_window_or_self_are_ignored():
    outside = make_anomaly(["catalogue"], "anom-t-2", ONSET + timedelta(seconds=121))
    report = score(["front-end"], related=[outside, make_anomaly(["front-end"])])
    assert report.related_anomaly_ids == []
    assert by_service(report)["catalogue"].signals.co_anomaly == 0.0


def test_incident_similarity_is_zero_without_retrieved_incidents():
    report = score(["front-end"])
    assert all(c.signals.incident_similarity == 0.0 for c in report.candidates)
    assert report.retrieval_status == "not_run"


def make_incident(incident_id, services, similarity):
    return SimilarIncident(
        incident_id=incident_id,
        title=f"title of {incident_id}",
        services=list(services),
        fault_type="db_pool_saturation",
        source="synthetic",
        similarity=similarity,
    )


def test_incident_similarity_is_the_best_incident_naming_the_candidate():
    incidents = [
        make_incident("incident-9001", ["catalogue"], 0.62),
        make_incident("incident-9002", ["catalogue", "orders"], 0.71),
        make_incident("incident-9003", [], 0.90),  # a public postmortem names no Sock Shop service
    ]
    inputs = ScoringInputs(
        anomaly=make_anomaly(["front-end"]), related=[], deploys=[], similar_incidents=incidents, retrieval_status="ok"
    )
    report = score_candidates(inputs, GRAPH, CONFIG)
    candidates = by_service(report)
    assert candidates["catalogue"].signals.incident_similarity == pytest.approx(0.71)
    assert candidates["orders"].signals.incident_similarity == pytest.approx(0.71)
    assert candidates["front-end"].signals.incident_similarity == 0.0
    assert "ev:anom-t-1:similar_incident:incident-9001" in candidates["catalogue"].evidence_ids
    assert not any("incident-9003" in item.evidence_id for item in report.evidence)
    assert report.retrieval_status == "ok"
    assert [i.incident_id for i in report.similar_incidents] == ["incident-9001", "incident-9002", "incident-9003"]


def test_negative_similarity_is_clipped_to_zero():
    inputs = ScoringInputs(
        anomaly=make_anomaly(["catalogue"]),
        related=[],
        deploys=[],
        similar_incidents=[make_incident("incident-9001", ["catalogue"], -0.2)],
    )
    report = score_candidates(inputs, GRAPH, CONFIG)
    assert by_service(report)["catalogue"].signals.incident_similarity == 0.0
    assert all(item.relevance >= 0 for item in report.evidence)


# ------------------------------------------------------------------ score and ranking


def test_score_is_the_weighted_sum_of_signals():
    report = score(["front-end", "orders"], [make_deploy("shipping", 2)])
    for candidate in report.candidates:
        expected = sum(w * getattr(candidate.signals, name) for name, w in CONFIG.weights.items())
        assert candidate.score == pytest.approx(expected)
    assert [c.rank for c in report.candidates] == list(range(1, len(report.candidates) + 1))
    assert [c.score for c in report.candidates] == sorted((c.score for c in report.candidates), reverse=True)


def test_weights_change_the_ranking():
    deploys = [make_deploy("catalogue", 1)]
    assert score(["front-end"], deploys).candidates[0].service == "catalogue"
    no_deploy_weight = dataclasses.replace(CONFIG, weight_deploy=0.0, weight_graph=0.65)
    assert score(["front-end"], deploys, config=no_deploy_weight).candidates[0].service == "front-end"


def test_ranking_is_deterministic_and_ties_break_by_name():
    first, second = score(["user", "carts"]).candidates[:2]
    assert (first.service, second.service) == ("carts", "user")
    assert first.score == second.score
    assert score(["user", "carts"]) == score(["user", "carts"])


# ------------------------------------------------------------------ evidence


@pytest.mark.parametrize("anomaly_id", sorted(FIXTURES))
def test_evidence_is_complete_and_consistent(anomaly_id):
    report = score_fixture(anomaly_id)
    evidence = {item.evidence_id: item for item in report.evidence}
    assert len(evidence) == len(report.evidence), "evidence ids must be unique"
    primary = f"ev:{anomaly_id}:anomaly:{anomaly_id}"
    cited = set()
    for candidate in report.candidates:
        assert candidate.evidence_ids[0] == primary
        assert set(candidate.evidence_ids) <= set(evidence), "a candidate cites evidence that isn't listed"
        cited |= set(candidate.evidence_ids)
        if candidate.deploy_id:
            assert f"ev:{anomaly_id}:deployment:{candidate.deploy_id}" in candidate.evidence_ids
        if candidate.distance:
            assert any(evidence[e].category == "dependency" for e in candidate.evidence_ids)
    assert cited == set(evidence), "listed evidence that no candidate cites"
    assert all(item.incident_id == anomaly_id for item in report.evidence)


def test_anomaly_evidence_reports_what_the_detector_measured():
    report = score_fixture("anom-fx-11")
    primary = next(i for i in report.evidence if i.source_id == "anom-fx-11")
    assert "payment liveness 31.06 (baseline 30)" in primary.summary
    assert primary.payload["detector"] == "staleness"
    assert primary.payload["contributors"][0]["baseline"] == 30.0


def test_anomaly_evidence_without_contributors_says_only_what_it_knows():
    report = score_fixture("anom-fx-01")
    primary = next(i for i in report.evidence if i.source_id == "anom-fx-01")
    assert primary.summary == "latency_p95_ms anomalous on catalogue (high)"
    assert primary.payload["contributors"] == []


def test_scoring_makes_no_network_calls(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("scoring attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    for anomaly_id in FIXTURES:
        score_fixture(anomaly_id)


# ------------------------------------------------------------------ fixtures

CRASH_REASON = (
    "known limitation: a crashed service stops reporting metrics, so it is never anomalous and "
    "has no deploy; ranking it needs a missing-metrics signal (PLAN.md, Phase 4 outcome). "
    "anom-fx-11 is the same fault seen through M2's staleness detector, which supplies exactly "
    "that signal, and it ranks correctly."
)


@pytest.mark.parametrize(
    "anomaly_id",
    [
        "anom-fx-01",
        "anom-fx-02",
        "anom-fx-03",
        "anom-fx-07",
        "anom-fx-08",
        "anom-fx-11",
        pytest.param("anom-fx-04", marks=pytest.mark.xfail(strict=True, reason=CRASH_REASON)),
        pytest.param("anom-fx-05", marks=pytest.mark.xfail(strict=True, reason=CRASH_REASON)),
        pytest.param("anom-fx-06", marks=pytest.mark.xfail(strict=True, reason=CRASH_REASON)),
    ],
)
def test_fixture_root_cause_ranks_first(anomaly_id):
    assert score_fixture(anomaly_id).candidates[0].service == FIXTURES[anomaly_id].meta.ground_truth_service


@pytest.mark.parametrize(
    "anomaly_id", [a for a, f in sorted(FIXTURES.items()) if f.meta.fault_type == "bad_deploy_latency"]
)
def test_bad_deploy_fixture_cites_the_injected_deploy(anomaly_id):
    top = score_fixture(anomaly_id).candidates[0]
    assert top.deploy_id == anomaly_id.replace("anom-fx-", "dep-fx-") + "-inj"


def test_benign_fixture_scores_below_a_real_fault():
    benign = score_fixture("anom-fx-09").candidates[0]
    assert benign.signals.deploy_proximity == 0.0
    assert benign.score < score_fixture("anom-fx-01").candidates[0].score


def test_ambiguous_fixture_is_a_tie():
    first, second = score_fixture("anom-fx-10").candidates[:2]
    assert {first.service, second.service} == {"carts", "user"}
    assert first.score == second.score


@pytest.mark.xfail(
    strict=True,
    reason="known limitation: with deploy weight 0.40, a background deploy to a symptom service "
    "minutes before onset outweighs the co-anomaly signal; weight tuning is Week 8 work",
)
def test_recent_deploy_to_a_symptom_does_not_outrank_a_no_deploy_cause():
    fx07_onset = FIXTURES["anom-fx-07"].event.t_onset
    recent = Deploy(
        deploy_id="dep-t-front-end-3",
        service="front-end",
        version="1.0.99",
        commit_sha="abc1234",
        time=fx07_onset - timedelta(minutes=3),
    )
    assert score_fixture("anom-fx-07", [recent]).candidates[0].service == "catalogue"
