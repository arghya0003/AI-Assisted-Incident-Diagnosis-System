"""
One operator console for the whole loop: inject a fault, decide what to do about
the diagnosis, read the history of both, and see the rate at which faults are
caught.

Why this is a server and not a static page: none of the services set CORS
headers, so a browser cannot call the fault injector and the orchestrator
directly from a page served anywhere else. Every call is proxied here instead,
which also keeps the database password out of the browser.

What this deliberately does NOT do: decide anything. The approve/reject tab posts
to M4's orchestrator and shows what it returns. The state machine, the audit
chain and the executor stay M4's, so there is one implementation of the decision
path and this is a second view onto it, not a second copy of it.
"""

from __future__ import annotations

import decimal
import logging
import os

import psycopg2
import psycopg2.extras
import requests
from flask import Flask, jsonify, request, send_from_directory

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("eval-dashboard")

app = Flask(__name__, static_folder="static", static_url_path="/static")

# Defaults are the host-published ports, so `python main.py` works against a
# running stack with no configuration. In Compose, override with service names.
INJECTOR_URL = os.environ.get("FAULT_INJECTOR_URL", "http://localhost:5001")
ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://localhost:8090")
DIAGNOSIS_URL = os.environ.get("DIAGNOSIS_URL", "http://localhost:8000")
LOAD_GENERATOR_URL = os.environ.get("LOAD_GENERATOR_URL", "http://localhost:5002")

PG = {
    "host": os.environ.get("PG_HOST", "localhost"),
    "port": os.environ.get("PG_PORT", "5432"),
    "dbname": os.environ.get("PG_DB", "metrics"),
    "user": os.environ.get("PG_USER", "postgres"),
    "password": os.environ.get("PG_PASSWORD", "Abcd1234#"),
}

# Mirrors KNOWN_SERVICES in services/fault-injector/main.py. The injector
# validates against its own list and does not publish it, so this is duplicated
# rather than fetched - the dashboard offering a service the injector rejects
# would be a worse failure than the duplication.
KNOWN_SERVICES = [
    "front-end", "catalogue", "payment", "user", "carts", "orders", "shipping",
]

# Fault windows are scored with the same 90s grace the evaluation runner uses,
# so "detected" means the same thing in this dashboard as in the report.
GRACE_SECONDS = 90
# A fault whose recovery was never recorded (injector died mid-run) would
# otherwise have an open-ended window swallowing every later anomaly.
MAX_WINDOW_MINUTES = 10


def connect():
    conn = psycopg2.connect(connect_timeout=5, **PG)
    conn.autocommit = True
    return conn


def query(sql: str, params=()) -> list[dict]:
    with connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]


def jsonable(rows: list[dict]) -> list[dict]:
    """Make timestamps and numerics JSON-safe without a custom encoder.

    Decimal matters more than it looks. Postgres returns `numeric` for EXTRACT
    and percentile_cont, psycopg2 maps that to Decimal, and Flask serialises
    Decimal as a JSON *string* - so a latency would arrive in the browser as
    "31.123" and the first `.toFixed()` on it would throw. Converting here keeps
    the fix in one place rather than coercing at every call site on the page.
    """
    out = []
    for row in rows:
        clean = {}
        for key, value in row.items():
            if hasattr(value, "isoformat"):
                clean[key] = value.isoformat()
            elif isinstance(value, decimal.Decimal):
                clean[key] = float(value)
            else:
                clean[key] = value
        out.append(clean)
    return out


def proxy(method: str, url: str, **kwargs):
    """Forward one call and pass the upstream status through unchanged.

    Upstream errors are surfaced, not translated: an operator seeing 409
    "incident is not awaiting approval" learns something, where a generic 500
    from this layer would hide which component refused and why.
    """
    try:
        response = requests.request(method, url, timeout=kwargs.pop("timeout", 15), **kwargs)
    except requests.RequestException as exc:
        return jsonify({"error": f"could not reach {url}: {exc}"}), 502
    try:
        return jsonify(response.json()), response.status_code
    except ValueError:
        return jsonify({"error": response.text[:500] or "empty response"}), response.status_code


# ------------------------------------------------------------------ the page

@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/api/health")
def health():
    """Per-backend reachability, so a blank tab is explained rather than puzzling."""
    checks = {}
    for name, url in (
        ("fault-injector", f"{INJECTOR_URL}/healthz"),
        ("orchestrator", f"{ORCHESTRATOR_URL}/health"),
        ("diagnosis-service", f"{DIAGNOSIS_URL}/health"),
        ("load-generator", f"{LOAD_GENERATOR_URL}/stats"),
    ):
        try:
            response = requests.get(url, timeout=3)
            checks[name] = {"ok": response.ok, "status": response.status_code}
        except requests.RequestException as exc:
            checks[name] = {"ok": False, "error": str(exc)[:120]}
    try:
        query("SELECT 1")
        checks["timescaledb"] = {"ok": True}
    except psycopg2.Error as exc:
        checks["timescaledb"] = {"ok": False, "error": str(exc)[:120]}

    # Offered load, because a fault injected into an idle testbed moves no
    # metric and nothing can detect it - the single most common way a run is
    # wasted. Surfaced on the inject tab before anyone presses the button.
    rps = None
    try:
        stats = requests.get(f"{LOAD_GENERATOR_URL}/stats", timeout=3).json()
        rps = stats.get("achieved_rps") or stats.get("offered_rps")
    except (requests.RequestException, ValueError):
        pass
    return jsonify({"checks": checks, "offered_rps": rps})


# ------------------------------------------------------------- tab 1: inject

@app.get("/api/fault-types")
def fault_types():
    try:
        types = requests.get(f"{INJECTOR_URL}/fault-types", timeout=5).json()
    except (requests.RequestException, ValueError) as exc:
        return jsonify({"error": f"could not reach the fault injector: {exc}"}), 502
    return jsonify({"fault_types": types, "services": KNOWN_SERVICES})


@app.post("/api/faults")
def inject():
    return proxy("POST", f"{INJECTOR_URL}/faults", json=request.get_json(force=True))


@app.get("/api/faults")
def faults():
    limit = min(int(request.args.get("limit", 20)), 200)
    return proxy("GET", f"{INJECTOR_URL}/faults", params={"limit": limit})


# ---------------------------------------------------- tab 2: human decisions

@app.get("/api/incidents")
def incidents():
    params = {"limit": min(int(request.args.get("limit", 50)), 500)}
    state = request.args.get("state")
    if state:
        params["state"] = state
    return proxy("GET", f"{ORCHESTRATOR_URL}/incidents", params=params)


@app.get("/api/actions")
def actions():
    return proxy("GET", f"{ORCHESTRATOR_URL}/actions")


@app.post("/api/incidents/<incident_id>/<decision>")
def decide(incident_id: str, decision: str):
    if decision not in ("approve", "reject", "request-info", "reanalyze"):
        return jsonify({"error": f"unknown decision {decision}"}), 400
    return proxy(
        "POST",
        f"{ORCHESTRATOR_URL}/incidents/{incident_id}/{decision}",
        json=request.get_json(force=True, silent=True) or {},
        timeout=30,
    )


# ------------------------------------------------------------- tab 3: history

# The fault window, repeated in each query below, is the same definition the
# evaluation runner scores with: from t_inject to recovery (capped), plus grace.
WINDOW = """
          t_detected BETWEEN s.t_inject AND
              LEAST(
                  COALESCE(s.t_recovered, s.t_inject + make_interval(mins => %(max_window)s)),
                  s.t_inject + make_interval(mins => %(max_window)s)
              ) + make_interval(secs => %(grace)s)
"""

HISTORY_SQL = """
SELECT
    s.scenario_id, s.fault_type, s.ground_truth_service, s.t_inject,
    s.t_recovered, s.status, s.params,
    a.anomaly_id, a.t_detected, a.severity,
    EXTRACT(EPOCH FROM (a.t_detected - s.t_inject)) AS detect_seconds,
    h.service AS top_service, h.cause AS top_cause,
    h.confidence, h.proposed_action,
    i.incident_id, i.state, i.decision, i.decided_hypothesis_rank,
    i.decided_by, i.decision_reason, i.execution_logged,
    chosen.proposed_action AS approved_action,
    chosen.service AS approved_service,
    audit.detail ->> 'verb' AS executed_verb,
    (audit.detail ->> 'executed')::boolean AS executed,
    audit.detail ->> 'blast_radius' AS blast_radius,
    audit.created_at AS executed_at
FROM fault_scenarios s
LEFT JOIN LATERAL (
    SELECT anomaly_id, t_detected, severity
    FROM anomalies
    WHERE s.ground_truth_service = ANY(services) AND """ + WINDOW + """
    ORDER BY t_detected ASC
    LIMIT 1
) a ON TRUE
LEFT JOIN LATERAL (
    SELECT service, cause, confidence, proposed_action
    FROM hypotheses
    WHERE anomaly_id = a.anomaly_id
    ORDER BY created_at DESC, rank ASC
    LIMIT 1
) h ON TRUE
LEFT JOIN LATERAL (
    -- orchestrator_incidents, NOT incidents: M3's diagnosis-service owns a table
    -- called `incidents` for its past-postmortem RAG corpus, which got the obvious
    -- name first and has no `state` column. M4's state machine is the one with the
    -- human decision on it. See the comment at the top of 008_incidents.sql.
    --
    -- Matched against every anomaly in the fault window, not just the earliest.
    -- One fault often raises several anomalies and the incident can be opened on
    -- a later one; keying this to `a.anomaly_id` lost the human decision whenever
    -- that happened, and the row claimed nobody had decided when somebody had.
    -- A decided incident wins over an undecided one, since the decision is the
    -- thing this column exists to show.
    SELECT oi.incident_id, oi.anomaly_id, oi.state, oi.decision,
           oi.decided_hypothesis_rank, oi.decided_by, oi.decision_reason,
           oi.execution_logged
    FROM orchestrator_incidents oi
    JOIN anomalies an ON an.anomaly_id = oi.anomaly_id
    WHERE s.ground_truth_service = ANY(an.services)
      AND an.t_detected BETWEEN s.t_inject AND
          LEAST(
              COALESCE(s.t_recovered, s.t_inject + make_interval(mins => %(max_window)s)),
              s.t_inject + make_interval(mins => %(max_window)s)
          ) + make_interval(secs => %(grace)s)
    ORDER BY (oi.decision IS NOT NULL) DESC, oi.created_at DESC
    LIMIT 1
) i ON TRUE
LEFT JOIN LATERAL (
    -- The hypothesis a human actually picked, which is not always rank 1 - an
    -- approver can choose a lower-ranked one, and that choice is the remedy that
    -- was agreed to.
    SELECT proposed_action, service
    FROM hypotheses
    WHERE anomaly_id = i.anomaly_id AND rank = i.decided_hypothesis_rank
    ORDER BY created_at DESC
    LIMIT 1
) chosen ON TRUE
LEFT JOIN LATERAL (
    -- What the executor recorded. It is stubbed by design and never acts, so
    -- `executed` is the field that keeps this honest: it says false.
    SELECT detail, created_at
    FROM audit_log
    WHERE incident_id = i.incident_id AND event_type = 'EXECUTION_INTENT_LOGGED'
    ORDER BY created_at DESC
    LIMIT 1
) audit ON TRUE
WHERE s.t_inject > now() - make_interval(hours => %(hours)s)
ORDER BY s.t_inject DESC
LIMIT %(limit)s
"""


@app.get("/api/history")
def history():
    """Every injected fault with what the system concluded and what a human decided.

    One row per fault rather than per anomaly: the fault is the ground truth, so
    a fault with no anomaly must still appear - those rows are the misses, and a
    history that only listed detected faults would be the most misleading view
    in the dashboard.
    """
    try:
        rows = query(HISTORY_SQL, {
            "hours": int(request.args.get("hours", 24)),
            "limit": min(int(request.args.get("limit", 200)), 1000),
            "grace": GRACE_SECONDS,
            "max_window": MAX_WINDOW_MINUTES,
        })
    except psycopg2.Error as exc:
        return jsonify({"error": f"database query failed: {exc}"}), 502
    return jsonify(jsonable(rows))


# --------------------------------------------------------------- tab 4: graph

RATE_SQL = """
SELECT
    time_bucket(make_interval(mins => %(bucket)s), s.t_inject) AS bucket,
    count(*) AS injected,
    count(a.anomaly_id) AS detected
FROM fault_scenarios s
LEFT JOIN LATERAL (
    SELECT anomaly_id FROM anomalies
    WHERE s.ground_truth_service = ANY(services) AND """ + WINDOW + """
    LIMIT 1
) a ON TRUE
WHERE s.t_inject > now() - make_interval(hours => %(hours)s)
GROUP BY bucket
ORDER BY bucket
"""

BY_TYPE_SQL = """
SELECT
    s.fault_type,
    count(*) AS injected,
    count(a.anomaly_id) AS detected,
    percentile_cont(0.5) WITHIN GROUP (
        ORDER BY EXTRACT(EPOCH FROM (a.t_detected - s.t_inject))
    ) AS median_detect_seconds
FROM fault_scenarios s
LEFT JOIN LATERAL (
    SELECT anomaly_id, t_detected FROM anomalies
    WHERE s.ground_truth_service = ANY(services) AND """ + WINDOW + """
    ORDER BY t_detected ASC
    LIMIT 1
) a ON TRUE
WHERE s.t_inject > now() - make_interval(hours => %(hours)s)
GROUP BY s.fault_type
ORDER BY injected DESC
"""

# Anomalies raised with no fault running. Not labelled "false positives": the
# testbed degrades on its own (issue #33), so some of these are real problems
# nobody injected. Calling them false would be a measurement error, and the
# evaluation report is where that argument belongs.
UNATTRIBUTED_SQL = """
SELECT
    time_bucket(make_interval(mins => %(bucket)s), an.t_detected) AS bucket,
    count(*) AS unattributed
FROM anomalies an
WHERE an.t_detected > now() - make_interval(hours => %(hours)s)
  AND NOT EXISTS (
      SELECT 1 FROM fault_scenarios s
      WHERE s.ground_truth_service = ANY(an.services)
        AND an.t_detected BETWEEN s.t_inject AND
            LEAST(
                COALESCE(s.t_recovered, s.t_inject + make_interval(mins => %(max_window)s)),
                s.t_inject + make_interval(mins => %(max_window)s)
            ) + make_interval(secs => %(grace)s)
  )
GROUP BY bucket
ORDER BY bucket
"""


@app.get("/api/fault-rate")
def fault_rate():
    args = {
        "hours": int(request.args.get("hours", 24)),
        "bucket": max(int(request.args.get("bucket", 30)), 1),
        "grace": GRACE_SECONDS,
        "max_window": MAX_WINDOW_MINUTES,
    }
    try:
        return jsonify({
            "series": jsonable(query(RATE_SQL, args)),
            "unattributed": jsonable(query(UNATTRIBUTED_SQL, args)),
            "by_type": jsonable(query(BY_TYPE_SQL, args)),
            "bucket_minutes": args["bucket"],
            "hours": args["hours"],
        })
    except psycopg2.Error as exc:
        return jsonify({"error": f"database query failed: {exc}"}), 502


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5010))
    log.info("dashboard on http://localhost:%d", port)
    app.run(host="0.0.0.0", port=port)
