"""
One row per detected anomaly: what it was, what caused it, what the system
concluded, what a human agreed to, and whether the problem has ended.

This is the "Recent" tab and the CSV dataset. Both come from the same rows, so
the file on disk and the page can never disagree about an anomaly.

Two things this module is careful about:

**"Resolved" is not "fixed".** The executor is stubbed by design and never acts
(services/orchestrator/app/executor.py), so no fix in this system has ever
restored a service. A row therefore carries the agreed fix and, separately,
what actually ended the problem - the injector withdrawing its fault, a silent
service reporting again, or the service simply going quiet. Collapsing the two
into "fixed by rollback_deploy:..." would be the most misleading thing the
dataset could say.

**An inferred resolution says it is inferred.** An injected fault has a recorded
recovery time; an anomaly nobody injected does not. For those, a service that
raises no further anomaly for QUIET_MINUTES is called resolved, with the basis
stated, because the detector's 120 s cooldown means one quiet cooldown proves
nothing but five in a row is a reasonable sign the problem has passed.

Pure functions only - no Flask, no database - so all of it is unit-tested.
"""

from __future__ import annotations

import csv
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("eval-dashboard.recent")

# Five times the anomaly detector's COOLDOWN_SECONDS (120). A service that is
# still broken re-alerts as soon as its cooldown ends, so silence shorter than a
# few cooldowns is not evidence of anything.
QUIET_MINUTES = 10

# The metrics table keeps 24 h of raw samples. Past that, "no sample since the
# service went silent" means the samples were dropped, not that it never came back.
METRICS_RETENTION_HOURS = 24

# The dataset's columns, in order. Changing this list changes the file's header;
# rows already on disk are rewritten into the new shape on the next export.
COLUMNS = [
    "anomaly_id", "t_detected", "t_onset", "detector", "services", "metrics", "severity",
    "cause_source", "scenario_id", "fault_type", "ground_truth_service", "t_inject",
    "detect_seconds",
    "diagnosed_service", "diagnosed_cause", "confidence", "diagnosis_correct",
    "proposed_action", "answered_by", "model_version",
    "incident_id", "incident_state",
    "decision", "fix", "fix_service", "fix_status", "decided_by", "decided_at", "decision_reason",
    "resolution", "resolved_at", "resolution_basis",
    "snapshot_at",
]

# The window an anomaly is attributed to an injected fault in. It is the same
# definition the history tab and the evaluation runner score with - from
# t_inject to recovery (capped), plus grace - so an anomaly counts as "caused by
# this fault" here exactly when it counts as "detected this fault" there.
RECENT_SQL = """
SELECT
    a.anomaly_id, a.detector, a.services, a.metrics, a.severity, a.t_onset, a.t_detected,
    s.scenario_id, s.fault_type, s.ground_truth_service, s.t_inject, s.t_recovered,
    s.status AS fault_status,
    oi.incident_id, oi.state AS incident_state, oi.decision, oi.decided_by, oi.decided_at,
    oi.decision_reason, oi.decided_hypothesis_rank, oi.hypotheses AS incident_hypotheses,
    oi.answered_by AS incident_answered_by, oi.model_version AS incident_model_version,
    h.service AS stored_service, h.cause AS stored_cause, h.confidence AS stored_confidence,
    h.proposed_action AS stored_action, h.answered_by AS stored_answered_by,
    h.model_version AS stored_model_version,
    (audit.detail ->> 'executed')::boolean AS executed,
    nxt.t_detected AS next_anomaly_at,
    back.time AS reporting_again_at
FROM anomalies a
LEFT JOIN LATERAL (
    SELECT s.scenario_id, s.fault_type, s.ground_truth_service, s.t_inject,
           s.t_recovered, s.status
    FROM fault_scenarios s
    WHERE s.ground_truth_service = ANY(a.services)
      AND a.t_detected BETWEEN s.t_inject AND
          LEAST(
              COALESCE(s.t_recovered, s.t_inject + make_interval(mins => %(max_window)s)),
              s.t_inject + make_interval(mins => %(max_window)s)
          ) + make_interval(secs => %(grace)s)
    ORDER BY s.t_inject DESC
    LIMIT 1
) s ON TRUE
LEFT JOIN LATERAL (
    -- orchestrator_incidents, not incidents: `incidents` is M3's RAG corpus.
    SELECT * FROM orchestrator_incidents
    WHERE anomaly_id = a.anomaly_id
    ORDER BY created_at DESC
    LIMIT 1
) oi ON TRUE
LEFT JOIN LATERAL (
    -- The diagnosis-service's own record, for anomalies the orchestrator never
    -- opened an incident for (it was down, or the anomaly predates it).
    SELECT h.service, h.cause, h.confidence, h.proposed_action, h.model_version,
           an.answered_by
    FROM hypotheses h
    LEFT JOIN analyses an ON an.analysis_id = h.analysis_id
    WHERE h.anomaly_id = a.anomaly_id AND h.rank = 1
    ORDER BY h.created_at DESC
    LIMIT 1
) h ON TRUE
LEFT JOIN LATERAL (
    SELECT detail FROM audit_log
    WHERE incident_id = oi.incident_id AND event_type = 'EXECUTION_INTENT_LOGGED'
    ORDER BY created_at DESC
    LIMIT 1
) audit ON TRUE
LEFT JOIN LATERAL (
    -- The next anomaly on any of the same services: did the problem come back?
    SELECT n.t_detected FROM anomalies n
    WHERE n.source = 'kafka' AND n.services && a.services AND n.t_detected > a.t_detected
    ORDER BY n.t_detected ASC
    LIMIT 1
) nxt ON TRUE
LEFT JOIN LATERAL (
    -- Only for liveness anomalies: the first sample from the silent service
    -- afterwards. The outer-only condition comes first so Postgres skips the
    -- metrics scan entirely for every other anomaly.
    SELECT m.time FROM metrics m
    WHERE 'liveness' = ANY(a.metrics)
      AND m.service = ANY(a.services) AND m.time > a.t_detected
    ORDER BY m.time ASC
    LIMIT 1
) back ON TRUE
WHERE a.source = 'kafka'
  AND (%(hours)s::int IS NULL OR a.t_detected > now() - make_interval(hours => %(hours)s::int))
ORDER BY a.t_detected DESC
LIMIT %(limit)s
"""


def _minutes(delta: timedelta) -> str:
    m = delta.total_seconds() / 60
    return f"{m:.0f} min" if m >= 1 else f"{delta.total_seconds():.0f} s"


def resolution(row: dict, now: datetime) -> tuple[str, datetime | None, str]:
    """(status, resolved_at, basis) for one anomaly.

    status is one of: resolved, ongoing, recurred, unknown.
    """
    detected = row["t_detected"]

    # An injected fault: the injector recorded when it ended, so this is a fact.
    if row.get("scenario_id"):
        if row.get("t_recovered"):
            return "resolved", row["t_recovered"], "injected fault withdrawn by the fault injector"
        if row.get("fault_status") == "running":
            return "ongoing", None, "injected fault still running"
        return "unknown", None, f"injector reported '{row.get('fault_status')}' with no recovery time"

    services = ", ".join(row.get("services") or []) or "the service"

    # A silent service: it is back when it reports again, whatever else happens.
    if "liveness" in (row.get("metrics") or []):
        if row.get("reporting_again_at"):
            return "resolved", row["reporting_again_at"], f"{services} reporting metrics again"
        if now - detected > timedelta(hours=METRICS_RETENTION_HOURS):
            return "unknown", None, "older than the 24 h metrics retention"
        return "ongoing", None, f"{services} still not reporting"

    nxt = row.get("next_anomaly_at")
    quiet = timedelta(minutes=QUIET_MINUTES)
    if nxt and nxt - detected <= quiet:
        return "recurred", None, f"{services} raised another anomaly {_minutes(nxt - detected)} later"
    if now - detected >= quiet:
        return "resolved", None, f"inferred: no further anomaly on {services} for {QUIET_MINUTES} min"
    return "ongoing", None, f"watching: under {QUIET_MINUTES} min since detection"


def _hypothesis(hyps: list[dict] | None, rank: int | None) -> dict | None:
    for h in hyps or []:
        if h.get("rank") == rank:
            return h
    return None


def diagnosis(row: dict) -> dict:
    """The rank-1 hypothesis - the one a human saw first.

    The incident's copy wins over the diagnosis-service's table, because the
    incident's is what was put in front of the approver; a later re-analysis in
    another mode can write newer rows to `hypotheses` that nobody decided on.
    """
    top = _hypothesis(row.get("incident_hypotheses"), 1)
    if top:
        return {
            "service": top.get("service"),
            "cause": top.get("cause"),
            "confidence": top.get("confidence"),
            "action": top.get("proposed_action"),
            "answered_by": row.get("incident_answered_by"),
            "model_version": row.get("incident_model_version"),
        }
    return {
        "service": row.get("stored_service"),
        "cause": row.get("stored_cause"),
        "confidence": row.get("stored_confidence"),
        "action": row.get("stored_action"),
        "answered_by": row.get("stored_answered_by"),
        "model_version": row.get("stored_model_version"),
    }


def fix_service(row: dict, diag: dict) -> str:
    """The service the approved hypothesis was about - the root cause a human agreed to.

    Not always the rank-1 service: an approver can pick a lower-ranked hypothesis, and then
    this is the label, not `diagnosed_service`. The diagnosis-service reads it as one
    (app/history.py), so it must never be filled for anything short of an approval.
    """
    if row.get("decision") != "approved":
        return ""
    chosen = _hypothesis(row.get("incident_hypotheses"), row.get("decided_hypothesis_rank"))
    return (chosen or {}).get("service") or diag.get("service") or ""


def fix(row: dict, diag: dict) -> tuple[str, str]:
    """(fix, fix_status): the remedy a human agreed to, and how far it got.

    `fix` is only ever filled for an approved decision. A proposal nobody
    approved is in `proposed_action`, not here, so the column answers "what was
    the fix" and nothing weaker.
    """
    state = row.get("incident_state")
    by = row.get("decided_by") or "unknown"
    if row.get("decision") == "approved":
        chosen = _hypothesis(row.get("incident_hypotheses"), row.get("decided_hypothesis_rank"))
        action = (chosen or {}).get("proposed_action") or diag.get("action") or ""
        rank = row.get("decided_hypothesis_rank")
        picked = f" (hypothesis #{rank})" if rank and rank != 1 else ""
        done = "executed" if row.get("executed") else "logged, not executed"
        return action, f"approved by {by}{picked} - {done}"
    if row.get("decision") == "rejected":
        return "", f"rejected by {by}"
    if state:
        return "", {
            "AWAITING_APPROVAL": "awaiting approval",
            "EXPIRED": "expired without a decision",
            "ANALYSIS_FAILED": "diagnosis failed",
            "DETECTED": "being diagnosed",
            "ANALYZING": "being diagnosed",
        }.get(state, state.lower())
    if diag.get("service"):
        return "", "diagnosed, but no incident was opened"
    return "", "no incident was opened"


def _iso(value) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else ("" if value is None else str(value))


def summarise(row: dict, now: datetime) -> dict:
    """One raw query row -> one dataset row, every value a string."""
    diag = diagnosis(row)
    status, resolved_at, basis = resolution(row, now)
    fix_action, fix_status = fix(row, diag)

    truth = row.get("ground_truth_service")
    correct = "" if not truth or not diag.get("service") else ("yes" if diag["service"] == truth else "no")
    detect = ""
    if row.get("t_inject"):
        detect = f"{(row['t_detected'] - row['t_inject']).total_seconds():.1f}"
    confidence = diag.get("confidence")

    return {
        "anomaly_id": row["anomaly_id"],
        "t_detected": _iso(row["t_detected"]),
        "t_onset": _iso(row.get("t_onset")),
        "detector": row.get("detector") or "",
        "services": ";".join(row.get("services") or []),
        "metrics": ";".join(row.get("metrics") or []),
        "severity": row.get("severity") or "",
        "cause_source": "injected_fault" if row.get("scenario_id") else "unattributed",
        "scenario_id": row.get("scenario_id") or "",
        "fault_type": row.get("fault_type") or "",
        "ground_truth_service": truth or "",
        "t_inject": _iso(row.get("t_inject")),
        "detect_seconds": detect,
        "diagnosed_service": diag.get("service") or "",
        "diagnosed_cause": diag.get("cause") or "",
        "confidence": "" if confidence is None else f"{float(confidence):.3f}",
        "diagnosis_correct": correct,
        "proposed_action": diag.get("action") or "",
        "answered_by": diag.get("answered_by") or "",
        "model_version": diag.get("model_version") or "",
        "incident_id": row.get("incident_id") or "",
        "incident_state": row.get("incident_state") or "",
        "decision": row.get("decision") or "",
        "fix": fix_action,
        "fix_service": fix_service(row, diag),
        "fix_status": fix_status,
        "decided_by": row.get("decided_by") or "",
        "decided_at": _iso(row.get("decided_at")),
        "decision_reason": row.get("decision_reason") or "",
        "resolution": status,
        "resolved_at": _iso(resolved_at),
        "resolution_basis": basis,
        "snapshot_at": _iso(now),
    }


def _sort_key(row: dict) -> datetime:
    try:
        return datetime.fromisoformat(row.get("t_detected", ""))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def read_dataset(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as f:
        return {r["anomaly_id"]: r for r in csv.DictReader(f) if r.get("anomaly_id")}


def write_dataset(path: Path, fresh: list[dict]) -> int:
    """Merge `fresh` rows into the CSV at `path` and return the row count.

    Rows already in the file but absent from `fresh` are kept. The database
    loses anomalies when a volume is wiped (`docker compose down -v`), and a
    dataset that silently shrank every time that happened would be no dataset
    at all. A kept row keeps its last-known resolution and its snapshot_at says
    when that was.

    Written to a temporary file and renamed into place, so a reader never sees
    half a file. If the rename fails - typically Excel holding the file open on
    Windows - the error propagates and the next export tries again.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = read_dataset(path)
    for r in fresh:
        rows[r["anomaly_id"]] = r
    ordered = sorted(rows.values(), key=_sort_key, reverse=True)

    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore", restval="")
        writer.writeheader()
        writer.writerows(ordered)
    os.replace(tmp, path)
    return len(ordered)
