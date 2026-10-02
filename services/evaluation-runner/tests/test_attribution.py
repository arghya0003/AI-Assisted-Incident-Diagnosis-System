import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from attribution import (  # noqa: E402
    HypothesisRow,
    cited_evidence_ids,
    classify_evidence_id,
    evidence_validity,
    rankings_for_scoring,
    ranked_services,
    score_attribution,
    service_from_cause,
)
from scoring import Scenario, mean_reciprocal_rank, top_k_accuracy  # noqa: E402

BASE = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)


def scenario(service="catalogue", fault_type="bad_deploy_latency", sid="sc-1"):
    return Scenario(
        scenario_id=sid,
        fault_type=fault_type,
        ground_truth_service=service,
        t_inject=BASE,
        t_recovered=None,
        status="recovered",
    )


def hyp(rank, service=None, cause="", evidence=(), confidence=0.5):
    return HypothesisRow(rank=rank, cause=cause, confidence=confidence,
                         evidence_ids=tuple(evidence), service=service)


# --------------------------------------------------------------- ranking

def test_ranked_services_orders_by_rank_not_input_order():
    ranked, inferred = ranked_services([
        hyp(3, "orders-db"), hyp(1, "catalogue"), hyp(2, "front-end"),
    ])
    assert ranked == ["catalogue", "front-end", "orders-db"]
    assert inferred == 0


def test_duplicate_services_collapse_to_their_best_rank():
    """Padding the list with repeats must not improve top-k."""
    ranked, _ = ranked_services([
        hyp(1, "front-end"), hyp(2, "catalogue"), hyp(3, "front-end"), hyp(4, "catalogue"),
    ])
    assert ranked == ["front-end", "catalogue"]


def test_missing_service_is_inferred_from_cause_and_counted():
    ranked, inferred = ranked_services([
        hyp(1, None, cause="catalogue is CPU-starved after a deploy"),
        hyp(2, "user"),
    ])
    assert ranked == ["catalogue", "user"]
    assert inferred == 1


def test_hypothesis_naming_nothing_recognisable_is_dropped_not_guessed():
    ranked, inferred = ranked_services([hyp(1, None, cause="unclear"), hyp(2, "user")])
    assert ranked == ["user"]
    assert inferred == 0


def test_longer_service_names_win_over_their_prefixes():
    """`catalogue-db` must never be read as `catalogue` — different culprit."""
    assert service_from_cause("catalogue-db is refusing connections") == "catalogue-db"
    assert service_from_cause("catalogue is slow") == "catalogue"


def test_service_from_cause_is_case_insensitive():
    assert service_from_cause("Front-End latency climbed") == "front-end"


# -------------------------------------------------------------- evidence

def test_classify_recognises_all_three_cited_shapes():
    assert classify_evidence_id("ev:anom-1:deployment:dep-2") == "evidence"
    assert classify_evidence_id("anom-20260930T120000-abc-0001") == "anomaly"
    assert classify_evidence_id("dep-2026-09-30-0001") == "deploy"
    assert classify_evidence_id("nonsense") is None


def test_cited_ids_are_deduplicated_across_hypotheses():
    """Every hypothesis cites the same anomaly; it must count once."""
    cited = cited_evidence_ids([
        hyp(1, "catalogue", evidence=("anom-1", "dep-9")),
        hyp(2, "user", evidence=("anom-1",)),
    ])
    assert cited == ["anom-1", "dep-9"]


def test_evidence_validity_counts_unresolved_citations():
    result = score_attribution(
        scenario(),
        [hyp(1, "catalogue", evidence=("anom-1", "anom-missing"))],
        anomaly_id="anom-1",
        resolved_ids={"anom-1"},
    )
    assert result.evidence_total == 2
    assert result.evidence_resolved == 1
    assert result.evidence_validity == 0.5
    assert result.unresolved_ids == ["anom-missing"]


def test_unchecked_evidence_is_not_measured_rather_than_zero():
    result = score_attribution(
        scenario(), [hyp(1, "catalogue", evidence=("anom-1",))],
        anomaly_id="anom-1", resolved_ids=None,
    )
    assert result.evidence_resolved == 0
    assert evidence_validity([result]) == 0.0  # citations exist, none confirmed
    no_citations = score_attribution(scenario(), [hyp(1, "catalogue")], anomaly_id="anom-1")
    assert no_citations.evidence_validity is None


# ---------------------------------------------------------------- scoring

def test_correct_top_rank_scores_one():
    result = score_attribution(
        scenario("catalogue"), [hyp(1, "catalogue"), hyp(2, "user")], anomaly_id="anom-1",
    )
    assert result.reciprocal_rank == 1.0
    assert result.scored


def test_second_place_scores_one_half():
    result = score_attribution(
        scenario("catalogue"), [hyp(1, "user"), hyp(2, "catalogue")], anomaly_id="anom-1",
    )
    assert result.reciprocal_rank == 0.5


def test_ground_truth_absent_scores_zero_but_is_still_scored():
    result = score_attribution(
        scenario("catalogue"), [hyp(1, "user"), hyp(2, "orders")], anomaly_id="anom-1",
    )
    assert result.reciprocal_rank == 0.0
    assert result.scored, "a wrong answer is a measurement, not a missing one"


def test_scenario_with_no_anomaly_is_excluded_from_attribution():
    """A detection miss must not be charged to the ranker."""
    result = score_attribution(scenario(), [], anomaly_id=None)
    assert not result.scored
    assert result.note and "never reached the ranker" in result.note
    assert rankings_for_scoring([result]) == []


def test_anomaly_without_hypotheses_is_excluded_and_explained():
    result = score_attribution(scenario(), [], anomaly_id="anom-1")
    assert not result.scored
    assert result.note == "anomaly found but no hypotheses stored"


def test_metrics_over_a_mixed_set_ignore_the_unscorable():
    results = [
        score_attribution(scenario("catalogue", sid="a"), [hyp(1, "catalogue")], "anom-1"),
        score_attribution(scenario("user", sid="b"),
                          [hyp(1, "orders"), hyp(2, "carts"), hyp(3, "user")], "anom-2"),
        score_attribution(scenario("payment", sid="c"), [], None),
    ]
    rankings = rankings_for_scoring(results)
    assert len(rankings) == 2
    assert mean_reciprocal_rank(rankings) == (1.0 + 1 / 3) / 2
    assert top_k_accuracy(rankings, 1) == 0.5
    assert top_k_accuracy(rankings, 3) == 1.0


def test_empty_result_set_reports_not_measured_rather_than_zero():
    assert mean_reciprocal_rank(rankings_for_scoring([])) is None
    assert evidence_validity([]) is None


# ------------------------------------------- issue #45: the three carried-over items

def dependency_scenario(service="catalogue", dependency="catalogue-db",
                        fault_type="dependency_timeout", sid="sc-dep"):
    return Scenario(
        scenario_id=sid,
        fault_type=fault_type,
        ground_truth_service=service,
        t_inject=BASE,
        t_recovered=None,
        status="recovered",
        params={"dependency": dependency, "duration_s": 90},
    )


def test_a_dependency_fault_is_scored_against_the_service_that_broke():
    """Pausing catalogue-db is labelled `catalogue`; naming catalogue-db is correct.

    Scoring against ground_truth_service would mark the right answer wrong and
    the symptom right (issue #45, item 1).
    """
    scenario = dependency_scenario()
    assert scenario.root_cause_service == "catalogue-db"
    result = score_attribution(
        scenario, [hyp(1, "catalogue-db"), hyp(2, "catalogue")], anomaly_id="anom-1",
    )
    assert result.reciprocal_rank == 1.0
    assert result.ground_truth_service == "catalogue-db"
    assert result.symptom_service == "catalogue", "detection still scores the symptom"


def test_naming_only_the_symptom_is_not_a_correct_diagnosis():
    result = score_attribution(
        dependency_scenario(), [hyp(1, "catalogue"), hyp(2, "catalogue-db")],
        anomaly_id="anom-1",
    )
    assert result.reciprocal_rank == 0.5


def test_an_ordinary_fault_is_unaffected_by_the_dependency_rule():
    """Only the dependency-shaped classes carry one, and only then does it differ."""
    plain = scenario("catalogue")
    assert plain.root_cause_service == "catalogue"
    same = Scenario(
        scenario_id="s", fault_type="bad_deploy_latency", ground_truth_service="catalogue",
        t_inject=BASE, params={"dependency": "catalogue"},
    )
    assert same.root_cause_service == "catalogue", (
        "a dependency equal to the service must not change the target"
    )


def test_incident_citations_resolve(monkeypatch):
    """M3's hypotheses cite past postmortems once retrieval works (issue #45, item 3)."""
    assert classify_evidence_id("incident-0014") == "incident"
    result = score_attribution(
        scenario(), [hyp(1, "catalogue", evidence=("anom-1", "incident-0014"))],
        anomaly_id="anom-1", resolved_ids={"anom-1", "incident-0014"},
    )
    assert result.evidence_validity == 1.0


def test_an_unknown_citation_shape_is_still_caught():
    """Adding `incident-` must not turn the metric into a rubber stamp."""
    assert classify_evidence_id("made-up-0001") is None
    result = score_attribution(
        scenario(), [hyp(1, "catalogue", evidence=("made-up-0001",))],
        anomaly_id="anom-1", resolved_ids=set(),
    )
    assert result.evidence_validity == 0.0
