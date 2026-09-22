"""The wire contract from CONTRACTS.md, and the /health and /analyze endpoints.

Endpoint tests use the in-memory store from conftest.py, which holds anom-0001 only.
"""

import copy
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.db import DatabaseUnavailable
from app.main import app, get_chat, get_embedder, get_store
from app.models import AnalyzeRequest, AnalyzeResponse, AnomalyEvent, SimilarIncident
from app.ollama import ChatReply, OllamaUnavailable

client = TestClient(app)

# Verbatim from CONTRACTS.md, "POST /analyze" response.
CONTRACT_RESPONSE = {
    "hypotheses": [
        {
            "rank": 1,
            "cause": "Bad deploy dep-2026-08-12-0007 to catalogue introduced a latency regression",
            "confidence": 0.81,
            "evidence_ids": ["anom-0001", "dep-2026-08-12-0007", "incident-0042"],
            "proposed_action": "rollback_deploy:dep-2026-08-12-0007",
        }
    ]
}

# A real event read off anomalies.detected during Phase 0 (2026-09-13).
REAL_M2_EVENT = {
    "anomaly_id": "anom-1789286893234",
    "services": ["catalogue"],
    "metrics": ["latency_p95_ms"],
    "severity": "high",
    "t_detected": "2026-09-13T08:08:13.234Z",
    "t_onset": "2026-09-13T08:08:13.221Z",
    "evidence_window": {"start": "2026-09-13T08:08:13.221Z", "end": "2026-09-13T08:08:13.221Z"},
}


def _with(**changes):
    """CONTRACT_RESPONSE with fields of its first hypothesis overridden."""
    body = copy.deepcopy(CONTRACT_RESPONSE)
    body["hypotheses"][0].update(changes)
    return body


# ---------------------------------------------------------------- models


def test_contract_example_response_is_valid():
    AnalyzeResponse.model_validate(CONTRACT_RESPONSE)


def test_real_m2_event_is_valid():
    event = AnomalyEvent.model_validate(REAL_M2_EVENT)
    assert event.services == ["catalogue"]
    # Zero-width window (issue #3) is accepted as-is, not rejected at ingestion.
    assert event.evidence_window.start == event.evidence_window.end


def test_anomaly_event_ignores_unknown_fields_from_m2():
    AnomalyEvent.model_validate({**REAL_M2_EVENT, "z_score": 4.2})


@pytest.mark.parametrize(
    "action",
    [
        "rollback_deploy:dep-2026-08-12-0007",
        "restart_service:catalogue",
        "scale_service:orders",
        "no_action",
    ],
)
def test_allowed_actions(action):
    AnalyzeResponse.model_validate(_with(proposed_action=action))


@pytest.mark.parametrize(
    "changes, reason",
    [
        ({"proposed_action": "delete_database:catalogue"}, "action outside vocabulary"),
        ({"proposed_action": "Roll back the catalogue deploy"}, "free-text action"),
        ({"proposed_action": "rollback_deploy:"}, "action missing its target"),
        ({"proposed_action": "no_action:catalogue"}, "no_action takes no target"),
        ({"confidence": 1.5}, "confidence above 1"),
        ({"confidence": -0.1}, "confidence below 0"),
        ({"evidence_ids": []}, "no evidence cited"),
        ({"evidence_ids": [""]}, "blank evidence id"),
        ({"rank": 0}, "rank below 1"),
        ({"reasoning": "because"}, "extra field an LLM might add"),
    ],
)
def test_invalid_hypotheses_are_rejected(changes, reason):
    with pytest.raises(ValidationError):
        AnalyzeResponse.model_validate(_with(**changes))


def test_missing_hypothesis_field_is_rejected():
    body = copy.deepcopy(CONTRACT_RESPONSE)
    del body["hypotheses"][0]["evidence_ids"]
    with pytest.raises(ValidationError):
        AnalyzeResponse.model_validate(body)


@pytest.mark.parametrize("ranks", [[1, 3], [1, 1], [2]])
def test_ranks_must_be_one_to_n(ranks):
    hypothesis = CONTRACT_RESPONSE["hypotheses"][0]
    body = {"hypotheses": [{**hypothesis, "rank": r} for r in ranks]}
    with pytest.raises(ValidationError):
        AnalyzeResponse.model_validate(body)


def test_ranks_may_arrive_unordered():
    hypothesis = CONTRACT_RESPONSE["hypotheses"][0]
    AnalyzeResponse.model_validate({"hypotheses": [{**hypothesis, "rank": r} for r in [2, 1]]})


@pytest.mark.parametrize("anomaly_id", ["", "   ", "anom 0001"])
def test_blank_or_spaced_anomaly_id_is_rejected(anomaly_id):
    with pytest.raises(ValidationError):
        AnalyzeRequest.model_validate({"anomaly_id": anomaly_id})


# ------------------------------------------------------------- endpoints


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["pipeline_mode"] == "full"
    assert len(resp.json()["config_fingerprint"]) == 12
    assert resp.json()["database"] == "ok"


def test_health_reports_an_empty_corpus(fake_store):
    """An empty corpus is otherwise invisible: retrieval silently contributes nothing and the
    service still answers, so a fresh volume can measure a retrieval-free system (issue #19)."""
    body = client.get("/health").json()
    assert body["corpus_incidents"] == 0
    assert body["retrieval"].startswith("empty_corpus")

    fake_store.incidents = [object(), object()]
    body = client.get("/health").json()
    assert body["corpus_incidents"] == 2 and body["retrieval"] == "ready"


def test_health_survives_a_database_outage(fake_store):
    fake_store.fail_counts = True
    body = client.get("/health").json()
    assert body["status"] == "ok", "a database outage must not fail the health check"
    assert body["corpus_incidents"] is None


def test_analyze_returns_contract_shape():
    resp = client.post("/analyze", json={"anomaly_id": "anom-0001"})
    assert resp.status_code == 200
    body = AnalyzeResponse.model_validate(resp.json())
    assert set(resp.json()) == {"hypotheses"}  # nothing beyond the contract
    assert body.hypotheses[0].rank == 1


def test_analyze_returns_the_llm_hypotheses():
    resp = client.post("/analyze", json={"anomaly_id": "anom-0001"})
    assert resp.headers["X-Diagnosis-Mode"] == "llm"
    assert resp.headers["X-LLM-Attempts"] == "1"
    hypothesis = resp.json()["hypotheses"][0]
    assert hypothesis["cause"] == "fake LLM cause"
    assert hypothesis["evidence_ids"] == ["anom-0001"]


def test_analyze_falls_back_when_the_llm_is_unreachable():
    def down(messages, schema):
        raise OllamaUnavailable("connection refused")

    app.dependency_overrides[get_chat] = lambda: down
    resp = client.post("/analyze", json={"anomaly_id": "anom-0001"})
    assert resp.status_code == 200  # M4 must never see a 500 because the model is down
    assert resp.headers["X-Diagnosis-Mode"] == "deterministic_fallback"
    body = AnalyzeResponse.model_validate(resp.json())
    assert body.hypotheses[0].cause.startswith("Deterministic ranking")
    assert body.hypotheses[0].evidence_ids[0] == "anom-0001"


def test_analyze_falls_back_when_the_llm_keeps_answering_invalid_json():
    calls = []

    def garbage(messages, schema):
        calls.append(messages)
        return ChatReply(content="I think the catalogue service is broken.")

    app.dependency_overrides[get_chat] = lambda: garbage
    resp = client.post("/analyze", json={"anomaly_id": "anom-0001"})
    assert resp.status_code == 200
    assert resp.headers["X-Diagnosis-Mode"] == "deterministic_fallback"
    assert resp.headers["X-LLM-Attempts"] == "3"
    assert len(calls) == 3


def test_analyze_unknown_anomaly_is_404():
    resp = client.post("/analyze", json={"anomaly_id": "anom-invented-9999"})
    assert resp.status_code == 404
    assert "anom-invented-9999" in resp.json()["detail"]


def test_analyze_database_down_is_503():
    app.dependency_overrides[get_store] = UnavailableStore
    assert client.post("/analyze", json={"anomaly_id": "anom-0001"}).status_code == 503
    health = client.get("/health")
    assert health.status_code == 200  # a DB outage must not fail the container healthcheck
    assert health.json()["database"] == "unreachable"


class UnavailableStore:
    def get(self, anomaly_id):
        raise DatabaseUnavailable("cannot reach TimescaleDB at timescaledb:5432")

    def analyses(self, anomaly_id, pipeline_mode=None, limit=20):
        raise DatabaseUnavailable("cannot reach TimescaleDB at timescaledb:5432")

    def scoring_inputs(self, anomaly_id, window_seconds, lookback_minutes):
        raise DatabaseUnavailable("cannot reach TimescaleDB at timescaledb:5432")

    def incident_count(self):
        raise DatabaseUnavailable("cannot reach TimescaleDB at timescaledb:5432")

    def status(self):
        return "unreachable"


def test_candidates_returns_the_ranked_breakdown():
    resp = client.get("/candidates/anom-0001")
    assert resp.status_code == 200
    body = resp.json()
    assert body["anomaly_id"] == "anom-0001"
    assert [c["rank"] for c in body["candidates"]] == list(range(1, len(body["candidates"]) + 1))
    assert {"catalogue", "front-end", "catalogue-db"} <= {c["service"] for c in body["candidates"]}
    assert set(body["candidates"][0]["signals"]) == set(body["weights"])


def test_candidates_unknown_anomaly_is_404():
    assert client.get("/candidates/anom-invented-9999").status_code == 404


def test_candidates_database_down_is_503():
    app.dependency_overrides[get_store] = UnavailableStore
    assert client.get("/candidates/anom-0001").status_code == 503


PAST_INCIDENT = SimilarIncident(
    incident_id="incident-0020",
    title="reporting job exhausts catalogue-db connections",
    services=["catalogue"],
    fault_type="db_pool_saturation",
    source="synthetic",
    similarity=0.7,
)


def test_candidates_reports_an_empty_corpus():
    body = client.get("/candidates/anom-0001").json()
    assert body["retrieval_status"] == "empty_corpus"
    assert body["similar_incidents"] == []


def test_candidates_uses_retrieved_incidents(fake_store):
    fake_store.incidents = [PAST_INCIDENT]
    body = client.get("/candidates/anom-0001").json()
    assert body["retrieval_status"] == "ok"
    assert [i["incident_id"] for i in body["similar_incidents"]] == ["incident-0020"]
    catalogue = next(c for c in body["candidates"] if c["service"] == "catalogue")
    assert catalogue["signals"]["incident_similarity"] == 0.7


def analyze(query=""):
    return client.post(f"/analyze{query}", json={"anomaly_id": "anom-0001"})


def test_analyze_mode_can_be_chosen_per_request():
    resp = analyze("?mode=deterministic")
    assert resp.status_code == 200
    assert resp.headers["X-Pipeline-Mode"] == "deterministic"
    assert resp.headers["X-Diagnosis-Mode"] == "deterministic"
    assert resp.headers["X-LLM-Attempts"] == "0"
    assert resp.json()["hypotheses"][0]["cause"].startswith("Deterministic ranking")


def test_analyze_rejects_an_unknown_mode():
    assert analyze("?mode=creative").status_code == 422


def test_analyze_stores_the_run_then_serves_it_again(fake_store):
    first = analyze()
    assert (first.headers["X-Cache"], first.headers["X-Persisted"]) == ("miss", "true")
    (analysis, evidence), = fake_store.saved
    assert analysis.analysis_id == first.headers["X-Analysis-Id"]
    assert analysis.answered_by == "llm" and analysis.hypotheses[0].service is not None
    assert evidence, "the evidence behind the ranking is stored with the run"

    second = analyze()
    assert second.headers["X-Cache"] == "hit"
    assert second.headers["X-Analysis-Id"] == first.headers["X-Analysis-Id"]
    assert second.json() == first.json()
    assert len(fake_store.saved) == 1

    third = analyze("?refresh=true")
    assert third.headers["X-Cache"] == "miss" and len(fake_store.saved) == 2


def test_the_cache_is_per_mode(fake_store):
    assert analyze().headers["X-Cache"] == "miss"
    assert analyze("?mode=deterministic").headers["X-Cache"] == "miss"
    assert analyze("?mode=deterministic").headers["X-Cache"] == "hit"


def test_a_fallback_is_never_served_from_the_cache(fake_store):
    def down(messages, schema):
        raise OllamaUnavailable("connection refused")

    app.dependency_overrides[get_chat] = lambda: down
    assert analyze().headers["X-Diagnosis-Mode"] == "deterministic_fallback"
    assert analyze().headers["X-Cache"] == "miss"  # the next request gets a fresh attempt
    assert len(fake_store.saved) == 2


def test_a_run_made_before_related_anomalies_could_arrive_is_not_reused(fake_store):
    now = datetime.now(timezone.utc)
    fresh = fake_store.events["anom-0001"].model_copy(update={"anomaly_id": "anom-fresh", "t_onset": now, "t_detected": now})
    fake_store.events["anom-fresh"] = fresh
    for _ in range(2):
        resp = client.post("/analyze", json={"anomaly_id": "anom-fresh"})
        assert resp.headers["X-Cache"] == "miss"


def test_a_failed_save_still_returns_the_answer(fake_store):
    fake_store.fail_saves = True
    resp = analyze()
    assert resp.status_code == 200 and resp.headers["X-Persisted"] == "false"
    assert resp.json()["hypotheses"]


def test_hypotheses_lists_stored_runs_newest_first(fake_store):
    analyze()
    analyze("?mode=deterministic")
    runs = client.get("/hypotheses/anom-0001").json()
    assert [run["pipeline_mode"] for run in runs] == ["deterministic", "full"]
    assert runs[0]["hypotheses"][0]["service"] and runs[0]["created_at"]
    only = client.get("/hypotheses/anom-0001?mode=full").json()
    assert [run["pipeline_mode"] for run in only] == ["full"]


def test_hypotheses_unknown_anomaly_is_404():
    assert client.get("/hypotheses/anom-invented-9999").status_code == 404


def test_hypotheses_database_down_is_503():
    app.dependency_overrides[get_store] = UnavailableStore
    assert client.get("/hypotheses/anom-0001").status_code == 503


def test_candidates_survives_an_unreachable_embedding_model(fake_store):
    fake_store.incidents = [PAST_INCIDENT]

    def down(texts):
        raise OllamaUnavailable("connection refused")

    app.dependency_overrides[get_embedder] = lambda: down
    resp = client.get("/candidates/anom-0001")
    assert resp.status_code == 200
    assert resp.json()["retrieval_status"] == "embedding_unavailable"
    assert all(c["signals"]["incident_similarity"] == 0.0 for c in resp.json()["candidates"])


@pytest.mark.parametrize(
    "body",
    [{}, {"anomaly_id": ""}, {"anomaly_id": "anom-0001", "extra": 1}, {"anomalyId": "anom-0001"}],
)
def test_analyze_rejects_malformed_requests(body):
    assert client.post("/analyze", json=body).status_code == 422
