"""The incident lifecycle: DETECTED -> ANALYZING -> AWAITING_APPROVAL -> APPROVED / REJECTED /
EXPIRED, with ANALYSIS_FAILED as the off-ramp when M3 cannot be reached (PLAN.md, M4
responsibilities). One `Orchestrator` instance is shared by the Kafka consumer thread, the
expiry-sweep thread and the REST API; every write goes through `app.db`, which keeps each
transition and its audit entry in one transaction, so concurrent callers cannot interleave a
partial state change.
"""

import logging
import uuid
from collections.abc import Callable

from app.db import IncidentStore
from app.diagnosis_client import DiagnosisClient, DiagnosisFailed
from app.executor import build_intent, execute
from app.models import AnomalyEvent, Incident

log = logging.getLogger("orchestrator.state_machine")

# Broadcast to WebSocket subscribers on every state change. Set by main.py at startup;
# a no-op default keeps this module testable without a live event loop.
Broadcaster = Callable[[dict], None]


def _noop_broadcast(_event: dict) -> None:
    return None


class Orchestrator:
    def __init__(self, store: IncidentStore, diagnosis: DiagnosisClient, broadcast: Broadcaster = _noop_broadcast):
        self._store = store
        self._diagnosis = diagnosis
        self._broadcast = broadcast

    def _emit(self, event_type: str, incident: Incident | None) -> None:
        self._broadcast({"event": event_type, "incident": incident.model_dump(mode="json") if incident else None})

    def handle_anomaly(self, event: AnomalyEvent) -> Incident | None:
        """Open an incident for a freshly detected anomaly and immediately try to analyze it.
        Idempotent: a re-delivered anomaly (consumer restart before offset commit) reuses the
        existing incident rather than opening a duplicate."""
        existing = self._store.get_by_anomaly(event.anomaly_id)
        if existing is not None:
            log.info("anomaly_id=%s already has incident %s (state=%s); skipping", event.anomaly_id, existing.incident_id, existing.state)
            return existing

        incident_id = f"inc-{uuid.uuid4().hex[:12]}"
        incident = self._store.create_detected(
            incident_id, event.anomaly_id, event.services, event.severity, event.model_dump(mode="json")
        )
        log.info("opened incident %s for anomaly_id=%s services=%s severity=%s", incident_id, event.anomaly_id, event.services, event.severity)
        self._emit("incident_detected", incident)
        self.analyze(incident_id)
        return self._store.get(incident_id)

    def analyze(self, incident_id: str) -> None:
        """DETECTED/ANALYSIS_FAILED -> ANALYZING -> AWAITING_APPROVAL, or -> ANALYSIS_FAILED
        again if M3 could not be reached or returned nothing usable after its own retries."""
        incident = self._store.get(incident_id)
        if incident is None:
            log.warning("analyze called for unknown incident %s", incident_id)
            return
        self._store.start_analysis(incident_id)
        self._emit("incident_analyzing", self._store.get(incident_id))

        try:
            result = self._diagnosis.analyze(incident.anomaly_id)
        except DiagnosisFailed as exc:
            log.error("incident %s: diagnosis failed (%s): %s", incident_id, exc.reason, exc.detail)
            self._store.fail_analysis(incident_id, f"{exc.reason}: {exc.detail}"[:1000])
            self._emit("incident_analysis_failed", self._store.get(incident_id))
            return

        self._store.complete_analysis(
            incident_id, result.analysis_id, result.model_version, result.answered_by,
            [h.model_dump() for h in result.hypotheses],
        )
        log.info("incident %s: analysis complete, %d hypotheses, answered_by=%s", incident_id, len(result.hypotheses), result.answered_by)
        self._emit("incident_awaiting_approval", self._store.get(incident_id))

    def approve(self, incident_id: str, hypothesis_rank: int, approver: str, note: str | None) -> Incident | None:
        """AWAITING_APPROVAL -> APPROVED, then hands the approved hypothesis's proposed_action
        to the stubbed executor (executor.py) -- logging only, never acting. Returns None if the
        incident was not awaiting approval (already decided, or expired out from under the
        caller); the API turns that into 404/409."""
        incident = self._store.get(incident_id)
        if incident is None or incident.state != "AWAITING_APPROVAL":
            return None
        chosen = next((h for h in incident.hypotheses if h.rank == hypothesis_rank), None)
        if chosen is None:
            raise ValueError(f"incident {incident_id} has no hypothesis ranked {hypothesis_rank}")

        decided = self._store.decide(incident_id, "approved", hypothesis_rank, approver, note)
        if decided is None:
            return None  # lost a race with expiry or another decision between the two reads above

        intent = build_intent(incident_id, chosen, approver)
        result = execute(intent)
        self._store.record_event("EXECUTION_INTENT_LOGGED", "orchestrator", result, incident_id=incident_id)
        self._store.mark_executed(incident_id)

        final = self._store.get(incident_id)
        self._emit("incident_approved", final)
        return final

    def reject(self, incident_id: str, approver: str, reason: str, reason_category: str, hypothesis_rank: int | None) -> Incident | None:
        incident = self._store.get(incident_id)
        if incident is None or incident.state != "AWAITING_APPROVAL":
            return None
        decided = self._store.decide(incident_id, "rejected", hypothesis_rank, approver, reason)
        if decided is None:
            return None
        self._store.save_rejection_feedback(incident_id, incident.anomaly_id, hypothesis_rank, reason_category, reason, approver)
        self._emit("incident_rejected", decided)
        return decided

    def request_info(self, incident_id: str, approver: str, note: str) -> Incident | None:
        updated = self._store.request_info(incident_id, approver, note)
        if updated is not None:
            self._emit("incident_more_info_requested", updated)
        return updated

    def reanalyze(self, incident_id: str) -> Incident | None:
        incident = self._store.get(incident_id)
        if incident is None or incident.state != "ANALYSIS_FAILED":
            return None
        self.analyze(incident_id)
        return self._store.get(incident_id)

    def sweep_expired(self) -> list[str]:
        expired = self._store.sweep_expired()
        for incident_id in expired:
            log.info("incident %s expired (no decision within the approval window)", incident_id)
            self._emit("incident_expired", self._store.get(incident_id))
        return expired
