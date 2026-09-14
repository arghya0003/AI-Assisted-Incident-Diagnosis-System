"""Phase 7: the evidence guardrail. Poisoned LLM replies citing ev-9999 and dep-fake-001 must never
reach a response, whatever else is in the reply."""

import json

import pytest
from fastapi.testclient import TestClient

from app.fixtures import load_fixtures
from app.graph import load_graph
from app.guardrail import GuardrailStats, allowed_ids, apply_guardrail
from app.hypotheses import Diagnosis
from app.llm import validate_reply
from app.main import app, get_chat, guardrail_stats
from app.models import AnalyzeResponse, Hypothesis
from app.ollama import ChatReply
from app.pipeline import DiagnosisPipeline, PipelineConfig
from app.prompts import build_prompt
from app.scoring import ScoringConfig, score_candidates
from app.settings import settings

GRAPH = load_graph()
SCORING = ScoringConfig.from_settings(settings)
CONFIG = PipelineConfig.from_settings(settings)
FIXTURES = {f.event.anomaly_id: f for f in load_fixtures()}
POISON = ["ev-9999", "dep-fake-001"]


def hypothesis(rank, evidence_ids, action="no_action"):
    return Hypothesis(rank=rank, cause=f"cause {rank}", confidence=0.9 - rank / 10, evidence_ids=evidence_ids, proposed_action=action)


def diagnosis(*items):
    """items: (service, evidence_ids) pairs, ranked in order."""
    return Diagnosis(
        response=AnalyzeResponse(hypotheses=[hypothesis(rank, ids) for rank, (_, ids) in enumerate(items, start=1)]),
        services=[service for service, _ in items],
    )


# ------------------------------------------------------------------ the filter


def test_poisoned_hypotheses_are_dropped_and_the_rest_re_ranked():
    allowed = frozenset({"anom-1", "dep-1"})
    result = apply_guardrail(
        diagnosis(("catalogue", ["anom-1", "ev-9999"]), ("front-end", ["anom-1"]), ("orders", ["dep-fake-001"]), ("user", ["anom-1", "dep-1"])),
        allowed,
    )
    assert result.checked == 4
    assert result.diagnosis.services == ["front-end", "user"]
    assert [h.rank for h in result.diagnosis.response.hypotheses] == [1, 2]
    assert [(r.rank, r.service, r.unknown_ids) for r in result.rejections] == [
        (1, "catalogue", ["ev-9999"]),
        (3, "orders", ["dep-fake-001"]),
    ]
    cited = {e for h in result.diagnosis.response.hypotheses for e in h.evidence_ids}
    assert not cited & set(POISON)


def test_a_single_unknown_id_drops_the_whole_hypothesis_without_repairing_it():
    result = apply_guardrail(diagnosis(("catalogue", ["anom-1", "dep-1", "ev-9999"])), frozenset({"anom-1", "dep-1"}))
    assert result.diagnosis.response.hypotheses == []  # not returned with the bad id stripped out
    assert result.rejections[0].unknown_ids == ["ev-9999"]


def test_a_clean_diagnosis_passes_unchanged():
    original = diagnosis(("catalogue", ["anom-1", "dep-1"]), ("user", ["anom-1"]))
    result = apply_guardrail(original, frozenset({"anom-1", "dep-1"}))
    assert result.rejections == [] and result.diagnosis.response == original.response


def test_the_allowed_set_is_what_the_service_supplied():
    fixture = FIXTURES["anom-fx-08"]
    report = score_candidates(fixture.scoring_inputs(), GRAPH, SCORING)
    prompt = build_prompt(fixture.event, report, context_tokens=100_000, response_reserve_tokens=1024)
    allowed = allowed_ids(report, prompt)
    assert "anom-fx-08" in allowed and "dep-fx-08-inj" in allowed
    assert {item.evidence_id for item in report.evidence} <= allowed
    assert set(prompt.citable_ids) <= allowed
    assert not set(POISON) & allowed
    # A deploy that exists in the data but was never put in front of the LLM is not citable.
    hidden = {c.deploy_id for c in report.candidates[5:] if c.deploy_id}
    assert not hidden & allowed


def test_without_a_prompt_the_report_evidence_is_the_allowed_set():
    fixture = FIXTURES["anom-fx-01"]
    report = score_candidates(fixture.scoring_inputs(), GRAPH, SCORING)
    assert {"anom-fx-01", "dep-fx-01-inj", "anom-fx-01-p99"} <= allowed_ids(report, None)


# ------------------------------------------------------------------ in the pipeline


class EmptyCorpus:
    def incident_count(self):
        return 0

    def search_incidents(self, *args, **kwargs):
        raise AssertionError("searched an empty corpus")


def poisoned_llm(poison_every_hypothesis):
    """Answers with every candidate. Validation can't catch this (service and action are legitimate);
    only the guardrail checks what the hypotheses cite."""

    def chat(messages, schema):
        variants = [v["properties"] for v in schema["properties"]["hypotheses"]["items"]["anyOf"]][:3]
        hypotheses = []
        for rank, variant in enumerate(variants, start=1):
            clean = variant["evidence_ids"]["items"]["enum"][:1]
            poisoned = poison_every_hypothesis or rank > 1
            hypotheses.append(
                {
                    "rank": rank,
                    "service": variant["service"]["enum"][0],
                    "cause": f"cause for {variant['service']['enum'][0]}",
                    "confidence": 0.9 - rank / 10,
                    "evidence_ids": clean + (POISON if poisoned else []),
                    "proposed_action": "no_action",
                }
            )
        return ChatReply(content=json.dumps({"hypotheses": hypotheses}))

    return chat


def run(anomaly_id, chat, stats=None):
    pipeline = DiagnosisPipeline(EmptyCorpus(), lambda texts: [], chat, GRAPH, SCORING, CONFIG, stats)
    return pipeline.analyze_inputs(FIXTURES[anomaly_id].scoring_inputs())


def cited(result):
    return {e for h in result.response.hypotheses for e in h.evidence_ids}


def test_validation_alone_would_let_the_poison_through():
    fixture = FIXTURES["anom-fx-02"]
    report = score_candidates(fixture.scoring_inputs(), GRAPH, SCORING)
    prompt = build_prompt(fixture.event, report, context_tokens=100_000, response_reserve_tokens=1024)
    reply = poisoned_llm(poison_every_hypothesis=True)(prompt.messages, prompt.schema)
    diagnosis_, errors = validate_reply(reply.content, prompt)
    assert errors == [] and diagnosis_ is not None


@pytest.mark.parametrize("anomaly_id", sorted(FIXTURES))
def test_poisoned_hypotheses_never_reach_the_response(anomaly_id):
    result = run(anomaly_id, poisoned_llm(poison_every_hypothesis=False))
    assert not cited(result) & set(POISON)
    assert result.mode == "llm"
    assert len(result.response.hypotheses) == 1  # only the clean rank-1 hypothesis survives
    assert all(set(r.unknown_ids) == set(POISON) for r in result.guardrail_rejections)


@pytest.mark.parametrize("anomaly_id", sorted(FIXTURES))
def test_a_fully_poisoned_reply_falls_back_to_the_deterministic_ranking(anomaly_id):
    result = run(anomaly_id, poisoned_llm(poison_every_hypothesis=True))
    assert not cited(result) & set(POISON)
    assert result.mode == "deterministic_fallback"
    assert "evidence guardrail rejected all" in result.fallback_reason
    assert result.response.hypotheses, "the fallback still answers"
    # Every hypothesis the LLM returned was rejected, and the fallback itself added no rejections.
    assert len(result.guardrail_rejections) == len(result.llm.diagnosis.services) >= 1


@pytest.mark.parametrize("anomaly_id", sorted(FIXTURES))
def test_the_deterministic_ranking_always_passes_the_guardrail(anomaly_id):
    def down(messages, schema):
        from app.ollama import OllamaUnavailable

        raise OllamaUnavailable("connection refused")

    result = run(anomaly_id, down)
    assert result.mode == "deterministic_fallback" and result.guardrail_rejections == []


def test_the_counters_record_llm_and_deterministic_hypotheses_separately():
    stats = GuardrailStats()
    run("anom-fx-08", poisoned_llm(poison_every_hypothesis=False), stats)
    run("anom-fx-08", poisoned_llm(poison_every_hypothesis=True), stats)
    counts = stats.snapshot()
    assert counts["llm_hypotheses_checked"] == 6
    assert counts["llm_hypotheses_rejected"] == 2 + 3
    assert counts["llm_responses_fully_rejected"] == 1
    assert counts["deterministic_hypotheses_checked"] == 3  # the fallback for the fully poisoned reply
    assert counts["deterministic_hypotheses_rejected"] == 0


# ------------------------------------------------------------------ over HTTP


def test_analyze_reports_rejections_in_a_header_and_in_stats():
    client = TestClient(app)
    before = client.get("/stats").json()["guardrail"]
    app.dependency_overrides[get_chat] = lambda: poisoned_llm(poison_every_hypothesis=False)
    resp = client.post("/analyze", json={"anomaly_id": "anom-0001"})
    assert resp.status_code == 200
    assert resp.headers["X-Guardrail-Rejected"] == "2"
    assert not {e for h in resp.json()["hypotheses"] for e in h["evidence_ids"]} & set(POISON)
    after = client.get("/stats").json()["guardrail"]
    assert after["llm_hypotheses_rejected"] - before["llm_hypotheses_rejected"] == 2
    assert guardrail_stats.snapshot() == after
