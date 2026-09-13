"""Phase 1: the wire contract from CONTRACTS.md, and the /health and /analyze endpoints."""

import copy

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.models import AnalyzeRequest, AnalyzeResponse, AnomalyEvent

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
    assert resp.json()["pipeline_mode"] == "stub"


def test_analyze_returns_contract_shape():
    resp = client.post("/analyze", json={"anomaly_id": "anom-0001"})
    assert resp.status_code == 200
    body = AnalyzeResponse.model_validate(resp.json())
    assert set(resp.json()) == {"hypotheses"}  # nothing beyond the contract
    assert body.hypotheses[0].rank == 1


def test_analyze_stub_is_honest():
    resp = client.post("/analyze", json={"anomaly_id": "anom-0001"})
    hypothesis = resp.json()["hypotheses"][0]
    assert resp.headers["X-Diagnosis-Mode"] == "stub"
    assert hypothesis["cause"].startswith("[stub]")
    assert hypothesis["confidence"] == 0.0
    assert hypothesis["proposed_action"] == "no_action"
    # Cites only the anomaly it was asked about, never invented evidence.
    assert hypothesis["evidence_ids"] == ["anom-0001"]


@pytest.mark.parametrize(
    "body",
    [{}, {"anomaly_id": ""}, {"anomaly_id": "anom-0001", "extra": 1}, {"anomalyId": "anom-0001"}],
)
def test_analyze_rejects_malformed_requests(body):
    assert client.post("/analyze", json=body).status_code == 422
