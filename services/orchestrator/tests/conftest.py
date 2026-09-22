"""Shared test setup. pytest imports this before any test module imports app.main.

FakeIncidentStore is an in-memory re-implementation of the IncidentStore protocol
(app/db.py), enforcing the same state-transition guards the real SQL does (e.g. `decide`
only succeeds from AWAITING_APPROVAL) so tests exercise the same contract PostgresIncidentStore
promises, without a live TimescaleDB.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.audit import GENESIS_HASH, hash_entry
from app.diagnosis_client import DiagnosisFailed, DiagnosisResult
from app.models import AuditEntry, Hypothesis, Incident


def now() -> datetime:
    return datetime.now(timezone.utc)


class FakeIncidentStore:
    def __init__(self):
        self._incidents: dict[str, dict] = {}
        self._audit: list[AuditEntry] = []
        self.rejections: list[dict] = []
        self.evidence: dict = {}  # source_id -> ResolvedEvidence, for resolve_evidence
        self.unavailable = False

    def _check(self):
        from app.db import DatabaseUnavailable
        if self.unavailable:
            raise DatabaseUnavailable("fake store is down")

    def _append_audit(self, incident_id: str | None, event_type: str, actor: str, detail: dict) -> AuditEntry:
        prev_hash = self._audit[-1].hash if self._audit else GENESIS_HASH
        audit_id = len(self._audit) + 1
        h = hash_entry(prev_hash, audit_id, incident_id, event_type, actor, detail)
        entry = AuditEntry(
            audit_id=audit_id, incident_id=incident_id, event_type=event_type, actor=actor,
            detail=detail, prev_hash=prev_hash, hash=h, created_at=now(),
        )
        self._audit.append(entry)
        return entry

    def _as_incident(self, row: dict) -> Incident:
        return Incident.model_validate(row)

    # ---- IncidentStore protocol ----

    def create_detected(self, incident_id, anomaly_id, services, severity, anomaly):
        self._check()
        if incident_id in self._incidents:
            return self._as_incident(self._incidents[incident_id])
        row = {
            "incident_id": incident_id, "anomaly_id": anomaly_id, "state": "DETECTED",
            "services": services, "severity": severity, "anomaly": anomaly,
            "hypotheses": [], "analysis_attempts": 0, "execution_logged": False,
            "created_at": now(), "updated_at": now(),
        }
        self._incidents[incident_id] = row
        self._append_audit(incident_id, "INCIDENT_DETECTED", "orchestrator", {"anomaly_id": anomaly_id})
        return self._as_incident(row)

    def get(self, incident_id):
        self._check()
        row = self._incidents.get(incident_id)
        return self._as_incident(row) if row else None

    def get_by_anomaly(self, anomaly_id):
        self._check()
        for row in self._incidents.values():
            if row["anomaly_id"] == anomaly_id:
                return self._as_incident(row)
        return None

    def list_incidents(self, state, limit):
        self._check()
        rows = [r for r in self._incidents.values() if state is None or r["state"] == state]
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return [self._as_incident(r) for r in rows[:limit]]

    def start_analysis(self, incident_id):
        self._check()
        row = self._incidents[incident_id]
        if row["state"] in ("DETECTED", "ANALYSIS_FAILED"):
            row["state"] = "ANALYZING"
            row["analysis_attempts"] += 1
            row["updated_at"] = now()
            self._append_audit(incident_id, "ANALYSIS_STARTED", "orchestrator", {})

    def complete_analysis(self, incident_id, analysis_id, model_version, answered_by, hypotheses):
        self._check()
        row = self._incidents[incident_id]
        if row["state"] != "ANALYZING":
            return
        row.update(
            state="AWAITING_APPROVAL", analysis_id=analysis_id, model_version=model_version,
            answered_by=answered_by, hypotheses=hypotheses, awaiting_since=now(),
            expires_at=now() + timedelta(seconds=1800), fail_reason=None, updated_at=now(),
        )
        self._append_audit(incident_id, "ANALYSIS_COMPLETE", "diagnosis-service", {"analysis_id": analysis_id})

    def fail_analysis(self, incident_id, reason):
        self._check()
        row = self._incidents[incident_id]
        if row["state"] != "ANALYZING":
            return
        row["state"] = "ANALYSIS_FAILED"
        row["fail_reason"] = reason
        row["updated_at"] = now()
        self._append_audit(incident_id, "ANALYSIS_FAILED", "orchestrator", {"reason": reason})

    def decide(self, incident_id, decision, hypothesis_rank, actor, reason):
        self._check()
        row = self._incidents[incident_id]
        if row["state"] != "AWAITING_APPROVAL":
            return None
        row.update(
            state="APPROVED" if decision == "approved" else "REJECTED", decision=decision,
            decided_hypothesis_rank=hypothesis_rank, decided_by=actor, decided_at=now(),
            decision_reason=reason, updated_at=now(),
        )
        self._append_audit(incident_id, "APPROVED" if decision == "approved" else "REJECTED", actor, {"hypothesis_rank": hypothesis_rank})
        return self._as_incident(row)

    def request_info(self, incident_id, actor, note):
        self._check()
        row = self._incidents[incident_id]
        if row["state"] != "AWAITING_APPROVAL":
            return None
        row["expires_at"] = now() + timedelta(seconds=1800)
        row["updated_at"] = now()
        self._append_audit(incident_id, "MORE_INFO_REQUESTED", actor, {"note": note})
        return self._as_incident(row)

    def mark_executed(self, incident_id):
        self._check()
        self._incidents[incident_id]["execution_logged"] = True

    def sweep_expired(self):
        self._check()
        expired = []
        for incident_id, row in self._incidents.items():
            if row["state"] == "AWAITING_APPROVAL" and row.get("expires_at") and row["expires_at"] < now():
                row["state"] = "EXPIRED"
                row["updated_at"] = now()
                self._append_audit(incident_id, "EXPIRED", "orchestrator", {"reason": "approval_timeout"})
                expired.append(incident_id)
        return expired

    def save_rejection_feedback(self, incident_id, anomaly_id, hypothesis_rank, reason_category, reason, approver):
        self._check()
        self.rejections.append({
            "incident_id": incident_id, "anomaly_id": anomaly_id, "hypothesis_rank": hypothesis_rank,
            "reason_category": reason_category, "reason": reason, "approver": approver,
        })

    def audit_trail(self, incident_id, limit):
        self._check()
        rows = [e for e in self._audit if incident_id is None or e.incident_id == incident_id]
        return list(reversed(rows))[:limit]

    def verify_audit_chain(self):
        self._check()
        expected_prev = GENESIS_HASH
        for entry in self._audit:
            if entry.prev_hash != expected_prev:
                return False, entry.audit_id
            recomputed = hash_entry(entry.prev_hash, entry.audit_id, entry.incident_id, entry.event_type, entry.actor, entry.detail)
            if recomputed != entry.hash:
                return False, entry.audit_id
            expected_prev = entry.hash
        return True, None

    def resolve_evidence(self, evidence_id):
        self._check()
        from app.models import ResolvedEvidence, parse_evidence_id

        category, source_id = parse_evidence_id(evidence_id)
        if category == "dependency" or "->" in source_id:
            caller, _, callee = source_id.partition("->")
            return ResolvedEvidence(
                evidence_id=evidence_id, kind="dependency",
                summary=f"{caller} calls {callee}", detail={"from": caller, "to": callee},
            )
        record = self.evidence.get(source_id)
        if record is None:
            return ResolvedEvidence(evidence_id=evidence_id, kind="unknown", summary="not found")
        return record.model_copy(update={"evidence_id": evidence_id})

    def record_event(self, event_type, actor, detail, incident_id=None):
        self._check()
        return self._append_audit(incident_id, event_type, actor, detail)

    def status(self):
        return "unreachable" if self.unavailable else "ok"


class FakeDiagnosisClient:
    """Stands in for app.diagnosis_client.DiagnosisClient. `answers` maps anomaly_id ->
    DiagnosisResult, or an exception instance/class to raise (DiagnosisFailed)."""

    def __init__(self):
        self.answers: dict[str, object] = {}
        self.calls: list[str] = []

    def analyze(self, anomaly_id: str) -> DiagnosisResult:
        self.calls.append(anomaly_id)
        outcome = self.answers.get(anomaly_id)
        if outcome is None:
            raise DiagnosisFailed("not_configured", f"no fake answer for {anomaly_id}")
        if isinstance(outcome, DiagnosisFailed):
            raise outcome
        return outcome

    def close(self):
        pass


def make_hypothesis(rank=1, action="rollback_deploy:dep-1", confidence=0.8) -> Hypothesis:
    return Hypothesis(rank=rank, cause="a cause", confidence=confidence, evidence_ids=["anom-1"], proposed_action=action)


@pytest.fixture
def fake_store() -> FakeIncidentStore:
    return FakeIncidentStore()


@pytest.fixture
def fake_diagnosis() -> FakeDiagnosisClient:
    return FakeDiagnosisClient()
