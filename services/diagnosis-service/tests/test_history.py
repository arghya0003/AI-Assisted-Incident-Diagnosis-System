"""Past outcomes from the anomaly dataset: which rows count, the leakage rules, selection, and how
they reach the prompt without loosening the per-candidate evidence binding."""

import csv
import dataclasses
import json
from datetime import timedelta

import pytest

from app.fixtures import load_fixtures
from app.graph import load_graph
from app.guardrail import allowed_ids
from app.history import HistoryStore, PastOutcome, parse_row, select
from app.llm import validate_reply
from app.prompts import build_prompt
from app.scoring import ScoringConfig, score_candidates
from app.settings import settings

GRAPH = load_graph()
CONFIG = ScoringConfig.from_settings(settings)
FIXTURE = {f.event.anomaly_id: f for f in load_fixtures()}["anom-fx-01"]  # catalogue latency
ANOMALY = FIXTURE.event
ONSET = ANOMALY.t_onset
BIG = {"context_tokens": 100_000, "response_reserve_tokens": 1024}


def iso(minutes_before_onset: float) -> str:
    return (ONSET - timedelta(minutes=minutes_before_onset)).isoformat()


def row(**over) -> dict:
    """A dataset row as the eval-dashboard writes it: an injected catalogue fault, ended."""
    base = {
        "anomaly_id": "anom-past-1", "t_onset": iso(60), "t_detected": iso(59.5),
        "detector": "ewma", "services": "catalogue", "metrics": "latency_p95_ms",
        "cause_source": "injected_fault", "scenario_id": "sc-1", "fault_type": "bad_deploy_latency",
        "ground_truth_service": "catalogue", "diagnosed_service": "catalogue", "diagnosis_correct": "yes",
        "decision": "", "fix": "", "fix_service": "", "decided_at": "", "decision_reason": "",
        "resolution": "resolved", "resolved_at": iso(58),
    }
    base.update(over)
    return base


def outcome(**over) -> PastOutcome:
    parsed = parse_row(row(**over))
    assert parsed is not None
    return parsed


# ------------------------------------------------------------- which rows count

def test_an_ended_injected_fault_is_ground_truth_known_when_it_ended():
    past = outcome()
    assert (past.label, past.root_cause, past.known_at.isoformat()) == ("injected", "catalogue", iso(58))


def test_an_injected_fault_still_running_is_not_a_label_yet():
    assert parse_row(row(resolution="ongoing", resolved_at="")) is None


def test_an_approval_labels_the_approved_service_not_the_rank_1_one():
    past = outcome(cause_source="unattributed", ground_truth_service="", resolved_at="",
                   diagnosed_service="catalogue", decision="approved", fix_service="catalogue-db",
                   fix="no_action", decided_at=iso(50))
    assert (past.label, past.root_cause, past.known_at.isoformat()) == ("approved", "catalogue-db", iso(50))


def test_a_rejection_is_a_negative_label_with_no_root_cause():
    past = outcome(cause_source="unattributed", ground_truth_service="", resolved_at="",
                   decision="rejected", decided_at=iso(50), decision_reason="wrong service")
    assert (past.label, past.root_cause, past.reason) == ("rejected", None, "wrong service")


def test_an_unverified_row_is_skipped_so_the_model_never_learns_from_its_own_guesses():
    # Unattributed, undecided: the only "label" would be this service's own diagnosis.
    assert parse_row(row(cause_source="unattributed", ground_truth_service="", resolved_at="")) is None
    assert parse_row(row(cause_source="unattributed", ground_truth_service="", decision="",
                         incident_state="EXPIRED")) is None


def test_a_naive_timestamp_is_read_as_utc_rather_than_breaking_comparisons():
    past = outcome(resolved_at=iso(58).replace("+00:00", ""))
    assert past.known_at.utcoffset() == timedelta(0)


# ----------------------------------------------------------------- leakage

def test_a_label_learned_after_onset_is_never_used():
    # The leak this module exists to prevent: the first anomaly of a fault that is still running
    # (or ended only after this anomaly began) carries this anomaly's own answer.
    late = outcome(resolved_at=(ONSET + timedelta(seconds=30)).isoformat())
    assert select([late], ANOMALY, 3) == []


def test_the_anomaly_itself_is_never_its_own_history():
    own = dataclasses.replace(outcome(), anomaly_id=ANOMALY.anomaly_id)
    assert select([own], ANOMALY, 3) == []


def test_a_past_outcome_on_no_shared_service_is_not_similar():
    assert select([outcome(services="payment", ground_truth_service="payment")], ANOMALY, 3) == []


# --------------------------------------------------------------- selection

def test_most_similar_first_and_one_per_fault():
    exact = outcome(anomaly_id="anom-a", scenario_id="sc-a")
    same_fault = outcome(anomaly_id="anom-a2", scenario_id="sc-a")
    looser = outcome(anomaly_id="anom-b", scenario_id="sc-b", services="catalogue;front-end",
                     metrics="cpu_rate", detector="zscore")
    chosen = select([looser, same_fault, exact], ANOMALY, 3)
    assert [p.anomaly_id for p in chosen][0] in ("anom-a", "anom-a2")
    assert len({p.group for p in chosen}) == len(chosen) == 2
    assert chosen[0].similarity > chosen[1].similarity


def test_top_k_zero_is_off():
    assert select([outcome()], ANOMALY, 0) == []


# ------------------------------------------------------------------ prompt

def prompt_with(history, **budget):
    report = score_candidates(FIXTURE.scoring_inputs(), GRAPH, CONFIG)
    return build_prompt(ANOMALY, report, history=history, **(budget or BIG)), report


def test_a_confirmed_outcome_is_shown_and_citable_only_by_the_candidate_it_names():
    past = select([outcome()], ANOMALY, 3)
    prompt, _ = prompt_with(past)
    text = prompt.messages[1]["content"]
    assert "PAST OUTCOMES IN THIS SYSTEM" in text
    assert "anom-past-1: latency_p95_ms on catalogue, 60 min earlier; root cause catalogue" in text
    assert "had blamed catalogue (correct)" in text
    options = {o.service: o for o in prompt.options}
    assert "anom-past-1" in options["catalogue"].citable_ids
    assert "anom-past-1" not in options["catalogue-db"].citable_ids


def test_fault_types_are_not_spelled_as_a_deploy_claim():
    prompt, _ = prompt_with(select([outcome()], ANOMALY, 3))
    assert "bad_deploy_latency" not in prompt.messages[1]["content"]
    assert "bad deploy latency fault" in prompt.messages[1]["content"]


def test_a_rejection_is_shown_but_citable_by_nobody():
    past = select([outcome(cause_source="unattributed", ground_truth_service="", resolved_at="",
                           decision="rejected", decided_at=iso(50))], ANOMALY, 3)
    prompt, _ = prompt_with(past)
    assert "an operator rejected the diagnosis of catalogue" in prompt.messages[1]["content"]
    assert all("anom-past-1" not in o.citable_ids for o in prompt.options)


def test_without_history_the_section_says_none_and_no_id_is_added():
    with_none, _ = prompt_with([])
    assert "PAST OUTCOMES IN THIS SYSTEM" in with_none.messages[1]["content"]
    assert all(not i.startswith("anom-past") for o in with_none.options for i in o.citable_ids)


def test_past_outcomes_are_the_first_thing_dropped_over_budget():
    past = select([outcome()], ANOMALY, 3)
    full, _ = prompt_with(past)
    without, _ = prompt_with([])
    # A budget the prompt fits only once history is gone.
    budget = {"context_tokens": without.estimated_tokens + 1024 + 5, "response_reserve_tokens": 1024}
    assert full.estimated_tokens > budget["context_tokens"] - 1024
    trimmed, _ = prompt_with(past, **budget)
    assert trimmed.truncations == ["dropped past outcomes"]
    assert all("anom-past-1" not in o.citable_ids for o in trimmed.options)


def test_the_guardrail_accepts_a_cited_outcome_and_validation_binds_it_to_its_candidate():
    prompt, report = prompt_with(select([outcome()], ANOMALY, 3))
    assert "anom-past-1" in allowed_ids(ANOMALY.anomaly_id, report, prompt)

    def reply(service):
        return json.dumps({"hypotheses": [{
            "rank": 1, "service": service, "cause": "latency_p95_ms rose on catalogue.",
            "confidence": 0.8, "evidence_ids": [ANOMALY.anomaly_id, "anom-past-1"], "proposed_action": "no_action",
        }]})

    ok, errors = validate_reply(reply("catalogue"), prompt)
    assert ok is not None and errors == []
    bad, errors = validate_reply(reply("catalogue-db"), prompt)
    assert bad is None and any("anom-past-1" in e for e in errors)


# ------------------------------------------------------------------- store

def write(path, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_store_reads_the_file_and_reports_what_it_could_use(tmp_path):
    path = tmp_path / "anomaly_dataset.csv"
    write(path, [row(), row(anomaly_id="anom-unverified", cause_source="unattributed",
                            ground_truth_service="", resolved_at="")])
    store = HistoryStore(path)
    assert [p.anomaly_id for p in store.select(ANOMALY, 3)] == ["anom-past-1"]
    assert store.status() == {"status": "ready", "path": str(path), "rows": 2, "confirmed": 1}


def test_store_without_a_file_is_empty_and_says_so(tmp_path):
    store = HistoryStore(tmp_path / "missing.csv")
    assert store.select(ANOMALY, 3) == []
    assert store.status()["status"] == "unavailable"
    assert HistoryStore(None).status() == {"status": "disabled"}


@pytest.mark.parametrize("mode,expected", [
    ("full", ["anom-past-1"]),
    ("no_graph", ["anom-past-1"]),
    # The deterministic baseline and the llm_only ablation must stay exactly what they measured.
    ("deterministic", []),
    ("llm_only", []),
])
def test_only_the_scored_llm_modes_carry_history(fake_store, mode, expected, tmp_path):
    from app.pipeline import DiagnosisPipeline, PipelineConfig
    from tests.conftest import fake_chat, fake_embed

    path = tmp_path / "anomaly_dataset.csv"
    write(path, [row()])
    config = PipelineConfig.from_settings(dataclasses.replace(settings, history_path=str(path)))
    pipeline = DiagnosisPipeline(fake_store, fake_embed, fake_chat, GRAPH, CONFIG, config,
                                 history=HistoryStore(path))
    result = pipeline.analyze_inputs(FIXTURE.scoring_inputs(), mode)
    assert result.past_outcomes == expected


def test_history_off_changes_the_config_fingerprint():
    from app.analyses import config_fingerprint
    from app.pipeline import PipelineConfig

    on = PipelineConfig.from_settings(dataclasses.replace(settings, history_path="x.csv", history_top_k=3))
    off = PipelineConfig.from_settings(dataclasses.replace(settings, history_path="x.csv", history_top_k=0))
    unset = PipelineConfig.from_settings(dataclasses.replace(settings, history_path="", history_top_k=3))
    assert off == unset  # no path and top_k 0 are the same configuration
    assert config_fingerprint("v", settings, CONFIG, on) != config_fingerprint("v", settings, CONFIG, off)
