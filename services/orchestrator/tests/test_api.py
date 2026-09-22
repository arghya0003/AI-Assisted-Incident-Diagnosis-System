import pytest
from fastapi.testclient import TestClient

from app.diagnosis_client import DiagnosisResult
from app.main import app, get_orchestrator, get_store
from app.models import AnomalyEvent
from app.state_machine import Orchestrator
from tests.conftest import FakeDiagnosisClient, FakeIncidentStore, make_hypothesis

client = TestClient(app)

ANOMALY = AnomalyEvent.model_validate({
    "anomaly_id": "anom-1",
    "services": ["catalogue"],
    "metrics": ["latency_p99_ms"],
    "severity": "high",
    "t_detected": "2026-08-12T20:45:00.123Z",
    "t_onset": "2026-08-12T20:44:30.000Z",
})


@pytest.fixture(autouse=True)
def override_dependencies():
    store = FakeIncidentStore()
    diagnosis = FakeDiagnosisClient()
    orch = Orchestrator(store, diagnosis)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_orchestrator] = lambda: orch
    yield store, diagnosis, orch
    app.dependency_overrides.clear()


def _open_incident(store, diagnosis, orch, action="rollback_deploy:dep-1"):
    diagnosis.answers[ANOMALY.anomaly_id] = DiagnosisResult(
        hypotheses=[make_hypothesis(rank=1, action=action)], analysis_id="a-1",
        model_version="phi4-mini", answered_by="llm",
    )
    return orch.handle_anomaly(ANOMALY)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["service"] == "orchestrator"


def test_actions_lists_the_fixed_vocabulary():
    response = client.get("/actions")
    body = response.json()
    assert set(body["actions_with_target"]) == {"rollback_deploy", "restart_service", "scale_service"}
    assert body["no_action"] == "no_action"
    assert "rollback_deploy" in body["blast_radius"]


def test_incident_not_found_is_404():
    response = client.get("/incidents/inc-does-not-exist")
    assert response.status_code == 404


def test_list_and_get_incident(override_dependencies):
    store, diagnosis, orch = override_dependencies
    incident = _open_incident(store, diagnosis, orch)

    listed = client.get("/incidents").json()
    assert any(i["incident_id"] == incident.incident_id for i in listed)

    got = client.get(f"/incidents/{incident.incident_id}")
    assert got.status_code == 200
    assert got.json()["state"] == "AWAITING_APPROVAL"


def test_approve_flow_end_to_end(override_dependencies):
    store, diagnosis, orch = override_dependencies
    incident = _open_incident(store, diagnosis, orch)

    response = client.post(
        f"/incidents/{incident.incident_id}/approve",
        json={"hypothesis_rank": 1, "approver": "alice", "note": "ship it"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "APPROVED"
    assert body["execution_logged"] is True

    audit = client.get(f"/incidents/{incident.incident_id}/audit").json()
    assert any(e["event_type"] == "EXECUTION_INTENT_LOGGED" for e in audit)


def test_approve_unknown_hypothesis_rank_is_422(override_dependencies):
    store, diagnosis, orch = override_dependencies
    incident = _open_incident(store, diagnosis, orch)
    response = client.post(
        f"/incidents/{incident.incident_id}/approve",
        json={"hypothesis_rank": 7, "approver": "alice"},
    )
    assert response.status_code == 422


def test_approve_twice_is_409(override_dependencies):
    store, diagnosis, orch = override_dependencies
    incident = _open_incident(store, diagnosis, orch)
    client.post(f"/incidents/{incident.incident_id}/approve", json={"hypothesis_rank": 1, "approver": "alice"})
    second = client.post(f"/incidents/{incident.incident_id}/approve", json={"hypothesis_rank": 1, "approver": "bob"})
    assert second.status_code == 409


def test_reject_flow(override_dependencies):
    store, diagnosis, orch = override_dependencies
    incident = _open_incident(store, diagnosis, orch)
    response = client.post(
        f"/incidents/{incident.incident_id}/reject",
        json={"approver": "alice", "reason": "wrong service", "reason_category": "wrong_root_cause", "hypothesis_rank": 1},
    )
    assert response.status_code == 200
    assert response.json()["state"] == "REJECTED"
    assert store.rejections[0]["reason_category"] == "wrong_root_cause"


def test_request_info_flow(override_dependencies):
    store, diagnosis, orch = override_dependencies
    incident = _open_incident(store, diagnosis, orch)
    response = client.post(
        f"/incidents/{incident.incident_id}/request-info",
        json={"approver": "alice", "note": "need the deploy diff"},
    )
    assert response.status_code == 200
    assert response.json()["state"] == "AWAITING_APPROVAL"


def test_extra_fields_are_rejected_on_approve(override_dependencies):
    store, diagnosis, orch = override_dependencies
    incident = _open_incident(store, diagnosis, orch)
    response = client.post(
        f"/incidents/{incident.incident_id}/approve",
        json={"hypothesis_rank": 1, "approver": "alice", "unexpected_field": "hack the executor"},
    )
    assert response.status_code == 422


def test_audit_verify_reports_intact_chain(override_dependencies):
    store, diagnosis, orch = override_dependencies
    _open_incident(store, diagnosis, orch)
    response = client.get("/audit/verify")
    assert response.json() == {"intact": True, "first_broken_audit_id": None}


def test_database_unavailable_returns_503(override_dependencies):
    store, diagnosis, orch = override_dependencies
    store.unavailable = True
    response = client.get("/incidents")
    assert response.status_code == 503
