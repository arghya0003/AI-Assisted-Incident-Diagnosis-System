from datetime import datetime, timedelta, timezone

import pytest

from app.diagnosis_client import DiagnosisFailed, DiagnosisResult
from app.models import AnomalyEvent
from app.state_machine import Orchestrator
from tests.conftest import make_hypothesis

ANOMALY = AnomalyEvent.model_validate({
    "anomaly_id": "anom-1",
    "services": ["catalogue"],
    "metrics": ["latency_p99_ms"],
    "severity": "high",
    "t_detected": "2026-08-12T20:45:00.123Z",
    "t_onset": "2026-08-12T20:44:30.000Z",
})


def _orchestrator(fake_store, fake_diagnosis, events=None):
    broadcast = (lambda e: events.append(e)) if events is not None else (lambda e: None)
    return Orchestrator(fake_store, fake_diagnosis, broadcast=broadcast)


def test_handle_anomaly_opens_and_analyzes_an_incident(fake_store, fake_diagnosis):
    fake_diagnosis.answers[ANOMALY.anomaly_id] = DiagnosisResult(
        hypotheses=[make_hypothesis(rank=1)], analysis_id="a-1", model_version="phi4-mini", answered_by="llm",
    )
    orch = _orchestrator(fake_store, fake_diagnosis)
    incident = orch.handle_anomaly(ANOMALY)

    assert incident.state == "AWAITING_APPROVAL"
    assert incident.anomaly_id == "anom-1"
    assert incident.services == ["catalogue"]
    assert len(incident.hypotheses) == 1
    assert incident.model_version == "phi4-mini"
    assert fake_diagnosis.calls == ["anom-1"]


def test_handle_anomaly_is_idempotent_on_anomaly_id(fake_store, fake_diagnosis):
    fake_diagnosis.answers[ANOMALY.anomaly_id] = DiagnosisResult(
        hypotheses=[make_hypothesis()], analysis_id="a-1", model_version="phi4-mini", answered_by="llm",
    )
    orch = _orchestrator(fake_store, fake_diagnosis)
    first = orch.handle_anomaly(ANOMALY)
    second = orch.handle_anomaly(ANOMALY)

    assert first.incident_id == second.incident_id
    assert len(fake_store._incidents) == 1
    assert fake_diagnosis.calls == ["anom-1"]  # not re-analyzed on the duplicate


def test_diagnosis_failure_leaves_incident_in_analysis_failed(fake_store, fake_diagnosis):
    fake_diagnosis.answers[ANOMALY.anomaly_id] = DiagnosisFailed("timeout", "diagnosis-service took too long")
    orch = _orchestrator(fake_store, fake_diagnosis)
    incident = orch.handle_anomaly(ANOMALY)

    assert incident.state == "ANALYSIS_FAILED"
    assert "timeout" in incident.fail_reason


def test_reanalyze_recovers_from_analysis_failed(fake_store, fake_diagnosis):
    fake_diagnosis.answers[ANOMALY.anomaly_id] = DiagnosisFailed("timeout", "slow")
    orch = _orchestrator(fake_store, fake_diagnosis)
    incident = orch.handle_anomaly(ANOMALY)
    assert incident.state == "ANALYSIS_FAILED"

    fake_diagnosis.answers[ANOMALY.anomaly_id] = DiagnosisResult(
        hypotheses=[make_hypothesis()], analysis_id="a-2", model_version="phi4-mini", answered_by="llm",
    )
    recovered = orch.reanalyze(incident.incident_id)
    assert recovered.state == "AWAITING_APPROVAL"
    assert recovered.analysis_attempts == 2


def test_reanalyze_refuses_from_a_non_failed_state(fake_store, fake_diagnosis):
    fake_diagnosis.answers[ANOMALY.anomaly_id] = DiagnosisResult(
        hypotheses=[make_hypothesis()], analysis_id="a-1", model_version="phi4-mini", answered_by="llm",
    )
    orch = _orchestrator(fake_store, fake_diagnosis)
    incident = orch.handle_anomaly(ANOMALY)
    assert orch.reanalyze(incident.incident_id) is None  # already AWAITING_APPROVAL


def test_approve_transitions_state_and_logs_execution_intent(fake_store, fake_diagnosis):
    fake_diagnosis.answers[ANOMALY.anomaly_id] = DiagnosisResult(
        hypotheses=[make_hypothesis(rank=1, action="rollback_deploy:dep-9")], analysis_id="a-1",
        model_version="phi4-mini", answered_by="llm",
    )
    events = []
    orch = _orchestrator(fake_store, fake_diagnosis, events)
    incident = orch.handle_anomaly(ANOMALY)

    approved = orch.approve(incident.incident_id, hypothesis_rank=1, approver="alice", note="looks right")
    assert approved.state == "APPROVED"
    assert approved.decided_by == "alice"
    assert approved.execution_logged is True

    audit_types = [e.event_type for e in fake_store.audit_trail(incident.incident_id, 50)]
    assert "EXECUTION_INTENT_LOGGED" in audit_types
    assert "APPROVED" in audit_types
    assert any(e["event"] == "incident_approved" for e in events)


def test_approve_rejects_an_unknown_hypothesis_rank(fake_store, fake_diagnosis):
    fake_diagnosis.answers[ANOMALY.anomaly_id] = DiagnosisResult(
        hypotheses=[make_hypothesis(rank=1)], analysis_id="a-1", model_version="phi4-mini", answered_by="llm",
    )
    orch = _orchestrator(fake_store, fake_diagnosis)
    incident = orch.handle_anomaly(ANOMALY)
    with pytest.raises(ValueError):
        orch.approve(incident.incident_id, hypothesis_rank=99, approver="alice", note=None)


def test_approve_refuses_an_already_decided_incident(fake_store, fake_diagnosis):
    fake_diagnosis.answers[ANOMALY.anomaly_id] = DiagnosisResult(
        hypotheses=[make_hypothesis(rank=1)], analysis_id="a-1", model_version="phi4-mini", answered_by="llm",
    )
    orch = _orchestrator(fake_store, fake_diagnosis)
    incident = orch.handle_anomaly(ANOMALY)
    orch.approve(incident.incident_id, hypothesis_rank=1, approver="alice", note=None)
    assert orch.approve(incident.incident_id, hypothesis_rank=1, approver="bob", note=None) is None


def test_reject_records_labelled_feedback(fake_store, fake_diagnosis):
    fake_diagnosis.answers[ANOMALY.anomaly_id] = DiagnosisResult(
        hypotheses=[make_hypothesis(rank=1)], analysis_id="a-1", model_version="phi4-mini", answered_by="llm",
    )
    orch = _orchestrator(fake_store, fake_diagnosis)
    incident = orch.handle_anomaly(ANOMALY)
    rejected = orch.reject(incident.incident_id, "alice", "wrong service entirely", "wrong_root_cause", 1)

    assert rejected.state == "REJECTED"
    assert fake_store.rejections[0]["reason_category"] == "wrong_root_cause"
    assert fake_store.rejections[0]["approver"] == "alice"


def test_request_info_extends_expiry_without_deciding(fake_store, fake_diagnosis):
    fake_diagnosis.answers[ANOMALY.anomaly_id] = DiagnosisResult(
        hypotheses=[make_hypothesis(rank=1)], analysis_id="a-1", model_version="phi4-mini", answered_by="llm",
    )
    orch = _orchestrator(fake_store, fake_diagnosis)
    incident = orch.handle_anomaly(ANOMALY)
    original_expiry = incident.expires_at

    updated = orch.request_info(incident.incident_id, "alice", "show me the deploy diff")
    assert updated.state == "AWAITING_APPROVAL"
    assert updated.expires_at >= original_expiry


def test_sweep_expires_incidents_past_their_deadline(fake_store, fake_diagnosis):
    fake_diagnosis.answers[ANOMALY.anomaly_id] = DiagnosisResult(
        hypotheses=[make_hypothesis(rank=1)], analysis_id="a-1", model_version="phi4-mini", answered_by="llm",
    )
    orch = _orchestrator(fake_store, fake_diagnosis)
    incident = orch.handle_anomaly(ANOMALY)
    fake_store._incidents[incident.incident_id]["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)

    expired = orch.sweep_expired()
    assert expired == [incident.incident_id]
    assert fake_store.get(incident.incident_id).state == "EXPIRED"


def test_approve_after_expiry_fails_cleanly(fake_store, fake_diagnosis):
    fake_diagnosis.answers[ANOMALY.anomaly_id] = DiagnosisResult(
        hypotheses=[make_hypothesis(rank=1)], analysis_id="a-1", model_version="phi4-mini", answered_by="llm",
    )
    orch = _orchestrator(fake_store, fake_diagnosis)
    incident = orch.handle_anomaly(ANOMALY)
    fake_store._incidents[incident.incident_id]["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    orch.sweep_expired()

    assert orch.approve(incident.incident_id, hypothesis_rank=1, approver="alice", note=None) is None
