"""Phase 6: the /analyze pipeline over every fixture, with a scripted LLM. Checks that the response
is always contract-valid, whether the LLM succeeds, misbehaves, or is unreachable."""

import dataclasses
import json

import pytest

from app.deterministic import CAUSE_PREFIX, deterministic_diagnosis
from app.fixtures import load_fixtures
from app.graph import load_graph
from app.models import AnalyzeResponse
from app.ollama import ChatReply, OllamaUnavailable
from app.pipeline import DiagnosisPipeline, PipelineConfig
from app.scoring import ScoringConfig, score_candidates
from app.settings import settings

GRAPH = load_graph()
SCORING = ScoringConfig.from_settings(settings)
CONFIG = PipelineConfig.from_settings(settings)
FIXTURES = load_fixtures()
IDS = [f.event.anomaly_id for f in FIXTURES]


class EmptyCorpus:
    def incident_count(self):
        return 0

    def search_incidents(self, *args, **kwargs):
        raise AssertionError("searched an empty corpus")

    def scoring_inputs(self, anomaly_id, window_seconds, lookback_minutes):
        return None


def pipeline(chat, config=CONFIG):
    return DiagnosisPipeline(EmptyCorpus(), lambda texts: [], chat, GRAPH, SCORING, config)


def top_candidate_llm(messages, schema):
    """A well-behaved LLM: one hypothesis about the top candidate with its last offered action."""
    top = schema["properties"]["hypotheses"]["items"]["anyOf"][0]["properties"]
    content = {
        "hypotheses": [
            {
                "rank": 1,
                "service": top["service"]["enum"][0],
                "cause": "scripted cause",
                "confidence": 0.6,
                "evidence_ids": top["evidence_ids"]["items"]["enum"],
                "proposed_action": top["proposed_action"]["enum"][-1],
            }
        ]
    }
    return ChatReply(content=json.dumps(content))


def unreachable_llm(messages, schema):
    raise OllamaUnavailable("connection refused")


def rambling_llm(messages, schema):
    return ChatReply(content="Probably a bad deploy, but I cannot be sure.")


@pytest.mark.parametrize("fixture", FIXTURES, ids=IDS)
def test_every_fixture_gets_an_llm_answer_when_the_llm_behaves(fixture):
    result = pipeline(top_candidate_llm).analyze_inputs(fixture.scoring_inputs())
    assert result.mode == "llm" and result.fallback_reason is None
    assert result.llm.attempts == 1
    assert result.response.hypotheses[0].cause == "scripted cause"
    assert result.services == [result.report.candidates[0].service]


@pytest.mark.parametrize("llm", [unreachable_llm, rambling_llm], ids=["unreachable", "invalid-json"])
@pytest.mark.parametrize("fixture", FIXTURES, ids=IDS)
def test_every_fixture_gets_a_valid_answer_when_the_llm_fails(fixture, llm):
    result = pipeline(llm).analyze_inputs(fixture.scoring_inputs())
    assert result.mode == "deterministic_fallback" and result.fallback_reason
    response = AnalyzeResponse.model_validate(result.response.model_dump())
    assert 1 <= len(response.hypotheses) <= 3
    options = {option.service: option for option in result.prompt.options}
    for hypothesis, service in zip(response.hypotheses, result.services):
        assert hypothesis.cause.startswith(CAUSE_PREFIX)
        assert hypothesis.evidence_ids[0] == fixture.event.anomaly_id
        # The fallback obeys the same per-candidate limits as the LLM.
        assert hypothesis.evidence_ids == options[service].citable_ids
        assert hypothesis.proposed_action in options[service].actions


def test_fallback_ranking_follows_the_deterministic_scores():
    fixture = next(f for f in FIXTURES if f.event.anomaly_id == "anom-fx-01")
    result = pipeline(unreachable_llm).analyze_inputs(fixture.scoring_inputs())
    top = result.response.hypotheses[0]
    assert result.services[0] == "catalogue"
    assert "dep-fx-01-inj" in top.cause
    assert top.proposed_action == "rollback_deploy:dep-fx-01-inj"
    assert top.confidence == pytest.approx(result.report.candidates[0].score, abs=0.001)
    assert top.evidence_ids == ["anom-fx-01", "dep-fx-01-inj", "anom-fx-01-p99"]


def test_fallback_does_not_propose_rolling_back_an_old_deploy():
    fixture = next(f for f in FIXTURES if f.event.anomaly_id == "anom-fx-07")
    report = score_candidates(fixture.scoring_inputs(), GRAPH, SCORING)
    diagnosis = deterministic_diagnosis(report)
    # fx-07's only deploys are 8 and 12 minutes old (scores below 0.5): cited, never rolled back.
    assert all(h.proposed_action == "no_action" for h in diagnosis.response.hypotheses)
    assert diagnosis.services == [c.service for c in report.candidates[:3]]


def test_a_prompt_over_budget_falls_back_without_calling_the_llm():
    calls = []

    def recording_llm(messages, schema):
        calls.append(messages)
        return top_candidate_llm(messages, schema)

    tiny = dataclasses.replace(CONFIG, context_tokens=1100, response_reserve_tokens=1024)
    result = pipeline(recording_llm, tiny).analyze_inputs(FIXTURES[0].scoring_inputs())
    assert result.mode == "deterministic_fallback" and "budget" in result.fallback_reason
    assert result.prompt is None and result.llm is None and calls == []


def test_unknown_anomaly_is_none():
    assert pipeline(top_candidate_llm).analyze("anom-does-not-exist") is None


# ------------------------------------------------------------------ pipeline modes

FIXTURES_BY_ID = {f.event.anomaly_id: f for f in FIXTURES}
MODES = ("full", "no_graph", "llm_only", "deterministic")


def never_called(messages, schema):
    raise AssertionError("the LLM was called")


class NoRetrieval(EmptyCorpus):
    def incident_count(self):
        raise AssertionError("retrieval ran")


@pytest.mark.parametrize("fixture", FIXTURES, ids=IDS)
def test_every_mode_returns_a_comparably_shaped_answer(fixture):
    for mode in MODES:
        chat = never_called if mode == "deterministic" else top_candidate_llm
        result = pipeline(chat).analyze_inputs(fixture.scoring_inputs(), mode)
        assert result.pipeline_mode == mode
        response = AnalyzeResponse.model_validate(result.response.model_dump())
        assert 1 <= len(response.hypotheses) <= 3
        assert response.hypotheses[0].evidence_ids[0] == fixture.event.anomaly_id
        assert len(result.services) == len(response.hypotheses)
        assert result.mode == ("deterministic" if mode == "deterministic" else "llm")


def test_deterministic_mode_is_the_baseline_not_a_fallback():
    result = pipeline(never_called).analyze_inputs(FIXTURES_BY_ID["anom-fx-01"].scoring_inputs(), "deterministic")
    assert result.mode == "deterministic"
    assert (result.fallback_reason, result.llm, result.prompt) == (None, None, None)
    assert result.services[0] == "catalogue"


def test_no_graph_mode_scores_without_graph_proximity():
    result = pipeline(top_candidate_llm).analyze_inputs(FIXTURES_BY_ID["anom-fx-08"].scoring_inputs(), "no_graph")
    assert result.report.weights["graph_proximity"] == 0.0
    full = pipeline(top_candidate_llm).analyze_inputs(FIXTURES_BY_ID["anom-fx-08"].scoring_inputs(), "full")
    assert full.report.weights["graph_proximity"] == 0.25


def test_llm_only_mode_skips_scoring_retrieval_and_the_graph():
    llm_only = DiagnosisPipeline(NoRetrieval(), never_called, top_candidate_llm, GRAPH, SCORING, CONFIG)
    result = llm_only.analyze_inputs(FIXTURES_BY_ID["anom-fx-01"].scoring_inputs(), "llm_only")
    assert result.mode == "llm" and result.report is None
    assert "score" not in result.prompt.messages[1]["content"].lower()


def test_llm_only_mode_has_no_deterministic_fallback():
    result = pipeline(unreachable_llm).analyze_inputs(FIXTURES_BY_ID["anom-fx-01"].scoring_inputs(), "llm_only")
    assert result.mode == "llm_failed"
    assert result.response.hypotheses == [] and result.services == []
    assert "connection refused" in result.fallback_reason
