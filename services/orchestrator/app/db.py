"""TimescaleDB access for the orchestrator's own tables (timescaledb/init/008_incidents.sql):
`orchestrator_incidents`, the immutable `audit_log`, and `rejection_feedback`.

The table is named `orchestrator_incidents`, not `incidents`: M3's diagnosis-service already
owns a table called `incidents` (005_diagnosis.sql) for its past-postmortem RAG corpus, an
unrelated concept that happened to get the same obvious name first. `CREATE TABLE IF NOT
EXISTS incidents` against that table would silently no-op and then fail on the first index
referencing a column M3's table doesn't have -- found by actually running this against the
live stack, not assumed away.

Every state transition and every audit entry that describes it are written in the same
transaction, so the audit trail can never show a decision the incidents table does not also
have (or vice versa). The API opens a short-lived connection per call, matching
diagnosis-service's own choice: this service is driven by incident-rate events (one Kafka
message, one human decision), not request-rate traffic, so a pool would only add stale-
connection handling for no benefit.
"""

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Literal, Protocol, TypeVar

import psycopg2
import psycopg2.errors
from psycopg2.extras import Json

from app.audit import GENESIS_HASH, hash_entry
from app.models import (
    AuditEntry,
    DeployRecord,
    Hypothesis,
    Incident,
    PastIncidentRecord,
    ResolvedEvidence,
    parse_evidence_id,
)
from app.settings import Settings

log = logging.getLogger("orchestrator.db")

M4_TABLES = ("orchestrator_incidents", "audit_log", "rejection_feedback")
MIGRATION = "timescaledb/init/008_incidents.sql"

DbStatus = Literal["ok", "unreachable", "schema_missing"]
T = TypeVar("T")


class DatabaseUnavailable(RuntimeError):
    """TimescaleDB is unreachable, or a table this service reads has not been created."""


def connect(settings: Settings, connect_timeout: int = 5):
    return psycopg2.connect(
        host=settings.pg_host,
        port=settings.pg_port,
        dbname=settings.pg_db,
        user=settings.pg_user,
        password=settings.pg_password,
        connect_timeout=connect_timeout,
    )


def _row_to_incident(row: tuple, columns: list[str]) -> Incident:
    data = dict(zip(columns, row))
    data["hypotheses"] = [Hypothesis.model_validate(h) for h in (data.get("hypotheses") or [])]
    return Incident.model_validate(data)


_INCIDENT_COLUMNS = """
    incident_id, anomaly_id, state, services, severity, anomaly, analysis_id, model_version,
    answered_by, hypotheses, analysis_attempts, fail_reason, decision, decided_hypothesis_rank,
    decided_by, decided_at, decision_reason, execution_logged, created_at, updated_at,
    awaiting_since, expires_at
"""


# ------------------------------------------------------------------ audit log


def _append_audit(cur, incident_id: str | None, event_type: str, actor: str, detail: dict) -> AuditEntry:
    """Append one row to the immutable audit log. Must run inside the caller's transaction so
    the row it writes and whatever incident-table change it describes commit together."""
    cur.execute("SELECT pg_advisory_xact_lock(hashtext('orchestrator_audit_log'))")
    cur.execute("SELECT audit_id, hash FROM audit_log ORDER BY audit_id DESC LIMIT 1")
    row = cur.fetchone()
    prev_audit_id, prev_hash = (row[0], row[1]) if row else (0, GENESIS_HASH)
    audit_id = prev_audit_id + 1
    entry_hash = hash_entry(prev_hash, audit_id, incident_id, event_type, actor, detail)
    cur.execute(
        """
        INSERT INTO audit_log (audit_id, incident_id, event_type, actor, detail, prev_hash, hash)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING created_at
        """,
        (audit_id, incident_id, event_type, actor, Json(detail), prev_hash, entry_hash),
    )
    (created_at,) = cur.fetchone()
    return AuditEntry(
        audit_id=audit_id, incident_id=incident_id, event_type=event_type, actor=actor,
        detail=detail, prev_hash=prev_hash, hash=entry_hash, created_at=created_at,
    )


def list_audit(cur, incident_id: str | None, limit: int) -> list[AuditEntry]:
    if incident_id is not None:
        cur.execute(
            """SELECT audit_id, incident_id, event_type, actor, detail, prev_hash, hash, created_at
               FROM audit_log WHERE incident_id = %s ORDER BY audit_id DESC LIMIT %s""",
            (incident_id, limit),
        )
    else:
        cur.execute(
            """SELECT audit_id, incident_id, event_type, actor, detail, prev_hash, hash, created_at
               FROM audit_log ORDER BY audit_id DESC LIMIT %s""",
            (limit,),
        )
    columns = [c.name for c in cur.description]
    return [AuditEntry.model_validate(dict(zip(columns, row))) for row in cur.fetchall()]


def verify_chain(cur) -> tuple[bool, int | None]:
    """Walk the whole audit log in order and recompute each hash. Returns (ok, first_broken_audit_id)."""
    cur.execute(
        "SELECT audit_id, incident_id, event_type, actor, detail, prev_hash, hash FROM audit_log ORDER BY audit_id ASC"
    )
    expected_prev = GENESIS_HASH
    for audit_id, incident_id, event_type, actor, detail, prev_hash, stored_hash in cur.fetchall():
        if prev_hash != expected_prev:
            return False, audit_id
        recomputed = hash_entry(prev_hash, audit_id, incident_id, event_type, actor, detail)
        if recomputed != stored_hash:
            return False, audit_id
        expected_prev = stored_hash
    return True, None


# ------------------------------------------------------------------ incidents


def get_incident(cur, incident_id: str) -> Incident | None:
    cur.execute(f"SELECT {_INCIDENT_COLUMNS} FROM orchestrator_incidents WHERE incident_id = %s", (incident_id,))
    row = cur.fetchone()
    if row is None:
        return None
    return _row_to_incident(row, [c.name for c in cur.description])


def get_incident_by_anomaly(cur, anomaly_id: str) -> Incident | None:
    """Idempotency guard: a re-delivered anomalies.detected message (consumer restart before
    a commit) must not open a second incident for the same anomaly_id."""
    cur.execute(f"SELECT {_INCIDENT_COLUMNS} FROM orchestrator_incidents WHERE anomaly_id = %s", (anomaly_id,))
    row = cur.fetchone()
    if row is None:
        return None
    return _row_to_incident(row, [c.name for c in cur.description])


def list_incidents(cur, state: str | None, limit: int) -> list[Incident]:
    if state is not None:
        cur.execute(
            f"SELECT {_INCIDENT_COLUMNS} FROM orchestrator_incidents WHERE state = %s ORDER BY created_at DESC LIMIT %s",
            (state, limit),
        )
    else:
        cur.execute(f"SELECT {_INCIDENT_COLUMNS} FROM orchestrator_incidents ORDER BY created_at DESC LIMIT %s", (limit,))
    columns = [c.name for c in cur.description]
    return [_row_to_incident(row, columns) for row in cur.fetchall()]


def create_detected(cur, incident_id: str, anomaly_id: str, services: list[str], severity: str, anomaly: dict) -> Incident:
    cur.execute(
        """
        INSERT INTO orchestrator_incidents (incident_id, anomaly_id, state, services, severity, anomaly)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (incident_id) DO NOTHING
        """,
        (incident_id, anomaly_id, "DETECTED", services, severity, Json(anomaly)),
    )
    if cur.rowcount == 1:
        _append_audit(cur, incident_id, "INCIDENT_DETECTED", "orchestrator", {
            "anomaly_id": anomaly_id, "services": services, "severity": severity,
        })
    return get_incident(cur, incident_id)


def mark_analyzing(cur, incident_id: str) -> None:
    cur.execute(
        "UPDATE orchestrator_incidents SET state = 'ANALYZING', analysis_attempts = analysis_attempts + 1, updated_at = now() "
        "WHERE incident_id = %s AND state IN ('DETECTED', 'ANALYSIS_FAILED')",
        (incident_id,),
    )
    if cur.rowcount == 1:
        _append_audit(cur, incident_id, "ANALYSIS_STARTED", "orchestrator", {})


def save_analysis_result(
    cur, incident_id: str, analysis_id: str, model_version: str, answered_by: str,
    hypotheses: list[dict], approval_timeout_seconds: float,
) -> None:
    cur.execute(
        """
        UPDATE orchestrator_incidents
        SET state = 'AWAITING_APPROVAL', analysis_id = %s, model_version = %s, answered_by = %s,
            hypotheses = %s, awaiting_since = now(), expires_at = now() + make_interval(secs => %s),
            fail_reason = NULL, updated_at = now()
        WHERE incident_id = %s AND state = 'ANALYZING'
        """,
        (analysis_id, model_version, answered_by, Json(hypotheses), approval_timeout_seconds, incident_id),
    )
    if cur.rowcount == 1:
        _append_audit(cur, incident_id, "ANALYSIS_COMPLETE", "diagnosis-service", {
            "analysis_id": analysis_id, "model_version": model_version, "answered_by": answered_by,
            "hypothesis_count": len(hypotheses),
        })


def mark_analysis_failed(cur, incident_id: str, reason: str) -> None:
    cur.execute(
        "UPDATE orchestrator_incidents SET state = 'ANALYSIS_FAILED', fail_reason = %s, updated_at = now() "
        "WHERE incident_id = %s AND state = 'ANALYZING'",
        (reason, incident_id),
    )
    if cur.rowcount == 1:
        _append_audit(cur, incident_id, "ANALYSIS_FAILED", "orchestrator", {"reason": reason})


def decide(
    cur, incident_id: str, decision: str, hypothesis_rank: int | None, actor: str, reason: str | None,
) -> Incident | None:
    """Transition AWAITING_APPROVAL -> APPROVED/REJECTED. Returns None if the incident is not
    (or no longer) awaiting approval -- the caller turns that into 404/409."""
    cur.execute(
        """
        UPDATE orchestrator_incidents
        SET state = %s, decision = %s, decided_hypothesis_rank = %s, decided_by = %s,
            decided_at = now(), decision_reason = %s, updated_at = now()
        WHERE incident_id = %s AND state = 'AWAITING_APPROVAL'
        """,
        ("APPROVED" if decision == "approved" else "REJECTED", decision, hypothesis_rank, actor, reason, incident_id),
    )
    if cur.rowcount != 1:
        return None
    _append_audit(cur, incident_id, "APPROVED" if decision == "approved" else "REJECTED", actor, {
        "hypothesis_rank": hypothesis_rank, "reason": reason,
    })
    return get_incident(cur, incident_id)


def log_request_info(cur, incident_id: str, actor: str, note: str, extend_seconds: float) -> Incident | None:
    cur.execute(
        "UPDATE orchestrator_incidents SET expires_at = now() + make_interval(secs => %s), updated_at = now() "
        "WHERE incident_id = %s AND state = 'AWAITING_APPROVAL'",
        (extend_seconds, incident_id),
    )
    if cur.rowcount != 1:
        return None
    _append_audit(cur, incident_id, "MORE_INFO_REQUESTED", actor, {"note": note})
    return get_incident(cur, incident_id)


def mark_execution_logged(cur, incident_id: str) -> None:
    cur.execute("UPDATE orchestrator_incidents SET execution_logged = true, updated_at = now() WHERE incident_id = %s", (incident_id,))


def expire_stale(cur) -> list[str]:
    cur.execute(
        "UPDATE orchestrator_incidents SET state = 'EXPIRED', updated_at = now() "
        "WHERE state = 'AWAITING_APPROVAL' AND expires_at IS NOT NULL AND expires_at < now() "
        "RETURNING incident_id"
    )
    expired = [row[0] for row in cur.fetchall()]
    for incident_id in expired:
        _append_audit(cur, incident_id, "EXPIRED", "orchestrator", {"reason": "approval_timeout"})
    return expired


def save_rejection_feedback(
    cur, incident_id: str, anomaly_id: str, hypothesis_rank: int | None, reason_category: str, reason: str, approver: str
) -> None:
    cur.execute(
        """
        INSERT INTO rejection_feedback (incident_id, anomaly_id, hypothesis_rank, reason_category, reason, approver)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (incident_id, anomaly_id, hypothesis_rank, reason_category, reason, approver),
    )


# ------------------------------------------------------------------ evidence resolution
#
# Read-only lookups into other members' tables: M1's `deploys`, M2's `anomalies`, and M3's
# `incidents` corpus. Each is wrapped so a missing table degrades to "unresolved" instead of
# failing the request -- an approver losing one evidence card is far better than the approval
# screen 503-ing because a teammate's migration has not been applied.


def _existing(cur, *tables: str) -> set[str]:
    cur.execute("SELECT t FROM unnest(%s::text[]) AS t WHERE to_regclass(t) IS NOT NULL", (list(tables),))
    return {table for (table,) in cur.fetchall()}


def resolve_evidence(cur, evidence_id: str) -> ResolvedEvidence:
    category, source_id = parse_evidence_id(evidence_id)

    # Two categories describe a relationship rather than a stored row, so they resolve without
    # touching the database at all.
    if category == "dependency" or "->" in source_id:
        caller, _, callee = source_id.partition("->")
        return ResolvedEvidence(
            evidence_id=evidence_id, kind="dependency",
            summary=f"{caller} calls {callee}; {callee} is on the anomalous request path.",
            detail={"from": caller, "to": callee},
        )

    if category == "metrics":
        service, _, rest = source_id.partition(":")
        metric, _, observed_at = rest.partition(":")
        return ResolvedEvidence(
            evidence_id=evidence_id, kind="metric",
            summary=f"{service} {metric} sampled at {observed_at}.",
            detail={"service": service, "metric": metric, "observed_at": observed_at},
        )

    present = _existing(cur, "deploys", "anomalies", "incidents")

    if "deploys" in present:
        cur.execute(
            "SELECT deploy_id, service, version, commit_sha, config_diff, time FROM deploys WHERE deploy_id = %s",
            (source_id,),
        )
        row = cur.fetchone()
        if row is not None:
            deploy = DeployRecord(**dict(zip(("deploy_id", "service", "version", "commit_sha", "config_diff", "time"), row)))
            return ResolvedEvidence(
                evidence_id=evidence_id, kind="deploy", deploy=deploy,
                summary=f"{deploy.service} {deploy.version} deployed {deploy.time:%Y-%m-%d %H:%M:%S} ({deploy.commit_sha}).",
            )

    if "anomalies" in present:
        cur.execute("SELECT raw FROM anomalies WHERE anomaly_id = %s", (source_id,))
        row = cur.fetchone()
        if row is not None:
            raw = row[0]
            return ResolvedEvidence(
                evidence_id=evidence_id, kind="anomaly", anomaly=raw,
                summary=f"Anomaly on {', '.join(raw.get('services', []))} "
                        f"({', '.join(raw.get('metrics', []))}), severity {raw.get('severity')}.",
            )

    # M3's corpus of past postmortems. Note this is `incidents`, M3's table -- not
    # `orchestrator_incidents`, which is this service's own lifecycle table.
    if "incidents" in present:
        cur.execute(
            "SELECT incident_id, title, body, services, fault_type, source FROM incidents WHERE incident_id = %s",
            (source_id,),
        )
        row = cur.fetchone()
        if row is not None:
            fields = dict(zip(("incident_id", "title", "body", "services", "fault_type", "source"), row))
            fields["services"] = fields["services"] or []
            past = PastIncidentRecord(**fields)
            return ResolvedEvidence(
                evidence_id=evidence_id, kind="past_incident", past_incident=past,
                summary=f"Past incident: {past.title}",
            )

    return ResolvedEvidence(
        evidence_id=evidence_id, kind="unknown",
        summary="This evidence id does not resolve to a deploy, anomaly or past incident on record.",
    )


def missing_tables(cur) -> list[str]:
    cur.execute("SELECT t FROM unnest(%s::text[]) AS t WHERE to_regclass(t) IS NULL", (list(M4_TABLES),))
    return [table for (table,) in cur.fetchall()]


class IncidentStore(Protocol):
    def create_detected(self, incident_id: str, anomaly_id: str, services: list[str], severity: str, anomaly: dict) -> Incident | None: ...
    def get(self, incident_id: str) -> Incident | None: ...
    def get_by_anomaly(self, anomaly_id: str) -> Incident | None: ...
    def list_incidents(self, state: str | None, limit: int) -> list[Incident]: ...
    def start_analysis(self, incident_id: str) -> None: ...
    def complete_analysis(self, incident_id: str, analysis_id: str, model_version: str, answered_by: str, hypotheses: list[dict]) -> None: ...
    def fail_analysis(self, incident_id: str, reason: str) -> None: ...
    def decide(self, incident_id: str, decision: str, hypothesis_rank: int | None, actor: str, reason: str | None) -> Incident | None: ...
    def request_info(self, incident_id: str, actor: str, note: str) -> Incident | None: ...
    def mark_executed(self, incident_id: str) -> None: ...
    def sweep_expired(self) -> list[str]: ...
    def save_rejection_feedback(self, incident_id: str, anomaly_id: str, hypothesis_rank: int | None, reason_category: str, reason: str, approver: str) -> None: ...
    def audit_trail(self, incident_id: str | None, limit: int) -> list[AuditEntry]: ...
    def resolve_evidence(self, evidence_id: str) -> ResolvedEvidence: ...
    def verify_audit_chain(self) -> tuple[bool, int | None]: ...
    def record_event(self, event_type: str, actor: str, detail: dict, incident_id: str | None = None) -> AuditEntry: ...
    def status(self) -> DbStatus: ...


class PostgresIncidentStore:
    def __init__(self, settings: Settings):
        self._settings = settings

    @contextmanager
    def _cursor(self) -> Iterator:
        try:
            conn = connect(self._settings)
        except psycopg2.OperationalError as exc:
            where = f"{self._settings.pg_host}:{self._settings.pg_port}"
            raise DatabaseUnavailable(f"cannot reach TimescaleDB at {where}") from exc
        try:
            with conn, conn.cursor() as cur:
                yield cur
        finally:
            conn.close()

    def _tx(self, fn: Callable[..., T], *args) -> T:
        try:
            with self._cursor() as cur:
                return fn(cur, *args)
        except (psycopg2.errors.UndefinedTable, psycopg2.errors.UndefinedColumn) as exc:
            raise DatabaseUnavailable(
                f"the database schema is out of date ({exc.diag.message_primary}); apply {MIGRATION}"
            ) from exc
        except psycopg2.OperationalError as exc:
            raise DatabaseUnavailable(f"TimescaleDB query failed: {exc}") from exc

    def create_detected(self, incident_id: str, anomaly_id: str, services: list[str], severity: str, anomaly: dict) -> Incident | None:
        return self._tx(create_detected, incident_id, anomaly_id, services, severity, anomaly)

    def get(self, incident_id: str) -> Incident | None:
        return self._tx(get_incident, incident_id)

    def get_by_anomaly(self, anomaly_id: str) -> Incident | None:
        return self._tx(get_incident_by_anomaly, anomaly_id)

    def list_incidents(self, state: str | None, limit: int) -> list[Incident]:
        return self._tx(list_incidents, state, limit)

    def start_analysis(self, incident_id: str) -> None:
        self._tx(mark_analyzing, incident_id)

    def complete_analysis(self, incident_id: str, analysis_id: str, model_version: str, answered_by: str, hypotheses: list[dict]) -> None:
        self._tx(
            lambda cur: save_analysis_result(
                cur, incident_id, analysis_id, model_version, answered_by, hypotheses,
                self._settings.approval_timeout_seconds,
            )
        )

    def fail_analysis(self, incident_id: str, reason: str) -> None:
        self._tx(mark_analysis_failed, incident_id, reason)

    def decide(self, incident_id: str, decision: str, hypothesis_rank: int | None, actor: str, reason: str | None) -> Incident | None:
        return self._tx(decide, incident_id, decision, hypothesis_rank, actor, reason)

    def request_info(self, incident_id: str, actor: str, note: str) -> Incident | None:
        return self._tx(log_request_info, incident_id, actor, note, self._settings.approval_timeout_seconds)

    def mark_executed(self, incident_id: str) -> None:
        self._tx(mark_execution_logged, incident_id)

    def sweep_expired(self) -> list[str]:
        return self._tx(expire_stale)

    def save_rejection_feedback(self, incident_id: str, anomaly_id: str, hypothesis_rank: int | None, reason_category: str, reason: str, approver: str) -> None:
        self._tx(save_rejection_feedback, incident_id, anomaly_id, hypothesis_rank, reason_category, reason, approver)

    def audit_trail(self, incident_id: str | None, limit: int) -> list[AuditEntry]:
        return self._tx(list_audit, incident_id, limit)

    def resolve_evidence(self, evidence_id: str) -> ResolvedEvidence:
        return self._tx(resolve_evidence, evidence_id)

    def verify_audit_chain(self) -> tuple[bool, int | None]:
        return self._tx(verify_chain)

    def record_event(self, event_type: str, actor: str, detail: dict, incident_id: str | None = None) -> AuditEntry:
        return self._tx(lambda cur: _append_audit(cur, incident_id, event_type, actor, detail))

    def status(self) -> DbStatus:
        try:
            with self._cursor() as cur:
                return "schema_missing" if missing_tables(cur) else "ok"
        except (DatabaseUnavailable, psycopg2.OperationalError):
            return "unreachable"
