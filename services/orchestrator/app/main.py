"""orchestrator HTTP + WebSocket API (M4).

Consumes `anomalies.detected` (M2), calls M3's `POST /analyze`, and persists an incident
through its full lifecycle: DETECTED -> ANALYZING -> AWAITING_APPROVAL -> APPROVED / REJECTED /
EXPIRED (or ANALYSIS_FAILED, if M3 could not be reached). Every remediation requires an explicit
human decision through this API -- nothing in this service, or anywhere downstream of it, ever
executes an action on the running system (app/executor.py).
"""

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect

from app import kafka_consumer, sweeper
from app.db import DatabaseUnavailable, IncidentStore, PostgresIncidentStore
from app.diagnosis_client import DiagnosisClient
from app.models import (
    ACTION_BLAST_RADIUS,
    ACTIONS_WITH_TARGET,
    NO_ACTION,
    ApproveRequest,
    AuditEntry,
    Incident,
    RejectRequest,
    RequestInfoRequest,
    ResolvedEvidence,
)
from app.settings import settings
from app.state_machine import Orchestrator
from app.ws import ConnectionManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("orchestrator")

SERVICE_VERSION = "0.1.0"
STARTED_AT = datetime.now(timezone.utc)

store = PostgresIncidentStore(settings)
diagnosis_client = DiagnosisClient(settings)
ws_manager = ConnectionManager()
orchestrator = Orchestrator(store, diagnosis_client, broadcast=ws_manager.publish)

_threads: list[tuple[threading.Thread, threading.Event]] = []


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ws_manager.bind_loop(asyncio.get_event_loop())
    log.info(
        "starting orchestrator %s diagnosis_service=%s approval_timeout=%.0fs",
        SERVICE_VERSION, settings.diagnosis_service_url, settings.approval_timeout_seconds,
    )
    _threads.append(kafka_consumer.start(orchestrator, settings))
    _threads.append(sweeper.start(orchestrator, settings))
    try:
        store.record_event("SERVICE_STARTED", "orchestrator", {"version": SERVICE_VERSION})
    except DatabaseUnavailable as exc:
        log.warning("could not record service-start audit event: %s", exc)
    yield
    for thread, stop in _threads:
        stop.set()
    diagnosis_client.close()


app = FastAPI(
    title="orchestrator",
    version=SERVICE_VERSION,
    description="M4: incident lifecycle, human-in-the-loop approval, and the safety architecture.",
    lifespan=lifespan,
)

ERROR_RESPONSES = {
    404: {"description": "No incident with this id"},
    409: {"description": "Incident is not in a state that allows this action"},
    503: {"description": "TimescaleDB unreachable or its schema out of date"},
}


def get_store() -> IncidentStore:
    return store


def get_orchestrator() -> Orchestrator:
    return orchestrator


def _unavailable(exc: DatabaseUnavailable) -> HTTPException:
    log.error("database unavailable: %s", exc)
    return HTTPException(status_code=503, detail=str(exc))


def _not_found(incident_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"unknown incident_id {incident_id!r}")


@app.get("/health")
def health(incidents: IncidentStore = Depends(get_store)) -> dict[str, str]:
    return {
        "status": "ok",
        "service": "orchestrator",
        "version": SERVICE_VERSION,
        "database": incidents.status(),
    }


@app.get("/actions")
def actions() -> dict[str, object]:
    """The fixed, enumerated action vocabulary and each verb's blast radius (PLAN.md safety
    architecture, item a). For the UI to render before an operator approves anything."""
    return {
        "actions_with_target": list(ACTIONS_WITH_TARGET),
        "no_action": NO_ACTION,
        "blast_radius": ACTION_BLAST_RADIUS,
    }


@app.get("/incidents", response_model=list[Incident])
def list_incidents(
    state: str | None = Query(None, description="Filter to one lifecycle state"),
    limit: int = Query(50, ge=1, le=500),
    incidents: IncidentStore = Depends(get_store),
) -> list[Incident]:
    try:
        return incidents.list_incidents(state, limit)
    except DatabaseUnavailable as exc:
        raise _unavailable(exc) from exc


@app.get("/incidents/{incident_id}", response_model=Incident, responses=ERROR_RESPONSES)
def get_incident(incident_id: str, incidents: IncidentStore = Depends(get_store)) -> Incident:
    try:
        incident = incidents.get(incident_id)
    except DatabaseUnavailable as exc:
        raise _unavailable(exc) from exc
    if incident is None:
        raise _not_found(incident_id)
    return incident


@app.get("/incidents/{incident_id}/audit", response_model=list[AuditEntry], responses=ERROR_RESPONSES)
def incident_audit(
    incident_id: str, limit: int = Query(200, ge=1, le=1000), incidents: IncidentStore = Depends(get_store)
) -> list[AuditEntry]:
    try:
        if incidents.get(incident_id) is None:
            raise _not_found(incident_id)
        return incidents.audit_trail(incident_id, limit)
    except DatabaseUnavailable as exc:
        raise _unavailable(exc) from exc


@app.get("/audit", response_model=list[AuditEntry])
def global_audit(limit: int = Query(200, ge=1, le=1000), incidents: IncidentStore = Depends(get_store)) -> list[AuditEntry]:
    """The whole immutable audit log, newest first -- who approved what, when, on what
    evidence, with which model version."""
    try:
        return incidents.audit_trail(None, limit)
    except DatabaseUnavailable as exc:
        raise _unavailable(exc) from exc


@app.get("/evidence/{evidence_id:path}", response_model=ResolvedEvidence, responses={503: ERROR_RESPONSES[503]})
def evidence(evidence_id: str, incidents: IncidentStore = Depends(get_store)) -> ResolvedEvidence:
    """Resolve one of a hypothesis's `evidence_ids` to the record behind it, so an approver can
    inspect the deploy diff or the past incident that justified a proposed action before
    approving it (PLAN.md: "Evidence must be inspectable").

    Uses `:path` because M3's structured ids contain slashes-free but colon-separated segments
    and, for `metrics`, an ISO timestamp — keeping the raw id intact matters more than tidy
    routing. An id that resolves to nothing returns `kind: "unknown"` rather than 404: the
    guardrail already guarantees cited ids existed at analysis time, so a miss means the record
    has since aged out, which the UI should say plainly rather than error on.
    """
    try:
        return incidents.resolve_evidence(evidence_id)
    except DatabaseUnavailable as exc:
        raise _unavailable(exc) from exc


@app.get("/audit/verify")
def audit_verify(incidents: IncidentStore = Depends(get_store)) -> dict[str, object]:
    """Walk the whole hash chain and report whether it is intact (app/audit.py)."""
    try:
        ok, broken_at = incidents.verify_audit_chain()
    except DatabaseUnavailable as exc:
        raise _unavailable(exc) from exc
    return {"intact": ok, "first_broken_audit_id": broken_at}


@app.post("/incidents/{incident_id}/approve", response_model=Incident, responses=ERROR_RESPONSES)
def approve(
    incident_id: str, request: ApproveRequest,
    orch: Orchestrator = Depends(get_orchestrator), incidents: IncidentStore = Depends(get_store),
) -> Incident:
    try:
        if incidents.get(incident_id) is None:
            raise _not_found(incident_id)
        try:
            incident = orch.approve(incident_id, request.hypothesis_rank, request.approver, request.note)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    except DatabaseUnavailable as exc:
        raise _unavailable(exc) from exc
    if incident is None:
        raise HTTPException(status_code=409, detail="incident is not awaiting approval")
    log.info("incident %s approved by %s (hypothesis rank %d)", incident_id, request.approver, request.hypothesis_rank)
    return incident


@app.post("/incidents/{incident_id}/reject", response_model=Incident, responses=ERROR_RESPONSES)
def reject(
    incident_id: str, request: RejectRequest,
    orch: Orchestrator = Depends(get_orchestrator), incidents: IncidentStore = Depends(get_store),
) -> Incident:
    try:
        if incidents.get(incident_id) is None:
            raise _not_found(incident_id)
        incident = orch.reject(incident_id, request.approver, request.reason, request.reason_category, request.hypothesis_rank)
    except DatabaseUnavailable as exc:
        raise _unavailable(exc) from exc
    if incident is None:
        raise HTTPException(status_code=409, detail="incident is not awaiting approval")
    log.info("incident %s rejected by %s: %s", incident_id, request.approver, request.reason)
    return incident


@app.post("/incidents/{incident_id}/request-info", response_model=Incident, responses=ERROR_RESPONSES)
def request_info(
    incident_id: str, request: RequestInfoRequest,
    orch: Orchestrator = Depends(get_orchestrator), incidents: IncidentStore = Depends(get_store),
) -> Incident:
    try:
        if incidents.get(incident_id) is None:
            raise _not_found(incident_id)
        incident = orch.request_info(incident_id, request.approver, request.note)
    except DatabaseUnavailable as exc:
        raise _unavailable(exc) from exc
    if incident is None:
        raise HTTPException(status_code=409, detail="incident is not awaiting approval")
    return incident


@app.post("/incidents/{incident_id}/reanalyze", response_model=Incident, responses=ERROR_RESPONSES)
def reanalyze(
    incident_id: str, orch: Orchestrator = Depends(get_orchestrator), incidents: IncidentStore = Depends(get_store)
) -> Incident:
    """Retry a failed analysis (ANALYSIS_FAILED -> ANALYZING -> ...) after transient
    diagnosis-service trouble, without waiting for another anomaly to arrive."""
    try:
        if incidents.get(incident_id) is None:
            raise _not_found(incident_id)
        incident = orch.reanalyze(incident_id)
    except DatabaseUnavailable as exc:
        raise _unavailable(exc) from exc
    if incident is None:
        raise HTTPException(status_code=409, detail="incident is not in ANALYSIS_FAILED")
    return incident


@app.websocket("/ws")
async def incident_feed(websocket: WebSocket) -> None:
    """Live incident feed: one JSON message per lifecycle event
    (`{"event": "...", "incident": {...}}`), for the approval UI's timeline."""
    queue = await ws_manager.connect(websocket)
    try:
        while True:
            event = await queue.get()
            await websocket.send_json(event)
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.disconnect(websocket)
