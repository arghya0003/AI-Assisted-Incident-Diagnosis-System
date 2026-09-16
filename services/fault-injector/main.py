"""
Fault injection harness (Phase 8, pulled forward). Runs real, physically
enforced faults against the Sock Shop testbed via the Docker Engine API
(mounted socket - Docker-outside-of-Docker), and records ground truth in
the shape M2's evaluation runner needs (CONTRACTS.md's "eval hooks":
scenario_id, fault_type, ground_truth_service, t_inject).

Three scenario types, chosen to be real (not simulated metrics) and cheap
to implement without re-architecting the testbed's network topology:

  bad_deploy_latency  - records a real deploy via deploy-emitter (Phase 5),
                        then CPU-throttles the target container via cgroups
                        (docker update --cpus equivalent) for the duration.
                        A deploy that quietly regresses per-request CPU
                        cost is a common real-world cause of latency
                        regressions, so this is a faithful mechanism, not
                        a shortcut - and it closes the loop with the
                        deploy log M3 will correlate against.
  service_crash       - stops the container, waits, restarts it. Simulates
                        a hard outage / dependency-cascade trigger.
  db_pool_saturation  - opens N held connections directly against
                        catalogue-db (MySQL) to exhaust its connection
                        limit, so `catalogue` starts failing to acquire
                        new ones. Mongo-backed services (carts/orders/user)
                        are a documented gap - see docs/phase8-fault-injection.md.
"""

import os
import json
import random
import logging
import threading
import time
from datetime import datetime, timezone

import docker
import psycopg2
import psycopg2.extras
import pymysql
import requests
from flask import Flask, request, jsonify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fault-injector")

PG_HOST = os.environ.get("PG_HOST", "timescaledb")
PG_PORT = os.environ.get("PG_PORT", "5432")
PG_DB = os.environ.get("PG_DB", "metrics")
PG_USER = os.environ.get("PG_USER", "postgres")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "Abcd1234#")

DEPLOY_EMITTER_URL = os.environ.get("DEPLOY_EMITTER_URL", "http://deploy-emitter:5000")
LOAD_GENERATOR_URL = os.environ.get("LOAD_GENERATOR_URL", "http://load-generator:5002")
COMPOSE_PROJECT = os.environ.get("COMPOSE_PROJECT_NAME", "incident-diagnosis-system")

MAX_DURATION_SECONDS = 300  # safety cap - a forgotten fault can't run forever
# catalogue-db runs with max_connections=151. The previous cap of 100 left 51
# connections free, so the "saturation" fault never actually saturated
# anything: an evaluation run measured catalogue's p95 at 5.7ms throughout,
# i.e. the fault was a no-op and every detector "missed" an incident that
# never happened. The cap now sits just above the pool so the fault can do
# what it claims; connections are still released in a finally block and the
# duration cap still bounds the blast radius.
MAX_CONNECTIONS = 160

# Below this the testbed is effectively idle (Prometheus scraping is most of
# it) and a fault won't show up in the metrics - warn rather than refuse, so
# a deliberate idle-baseline run is still possible.
MIN_USEFUL_RPS = 1.0

FAULT_TYPES = {"bad_deploy_latency", "service_crash", "db_pool_saturation"}
KNOWN_SERVICES = ["front-end", "catalogue", "payment", "user", "carts", "orders", "shipping"]

docker_client = docker.from_env()


def connect_postgres():
    while True:
        try:
            conn = psycopg2.connect(
                host=PG_HOST, port=PG_PORT, dbname=PG_DB,
                user=PG_USER, password=PG_PASSWORD,
            )
            conn.autocommit = True
            return conn
        except psycopg2.OperationalError as exc:
            log.warning("timescaledb not reachable yet (%s), retrying in 3s", exc)
            time.sleep(3)


conn = connect_postgres()
app = Flask(__name__)


def ensure_db_connection():
    global conn
    if conn is None or conn.closed:
        log.warning("postgres connection closed; reconnecting")
        conn = connect_postgres()
    return conn


def get_container(service: str):
    matches = docker_client.containers.list(
        filters={"label": f"com.docker.compose.service={service}"}
    )
    if not matches:
        raise ValueError(f"no running container found for service {service!r}")
    return matches[0]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def record_scenario(scenario_id, fault_type, service, params) -> None:
    db = ensure_db_connection()
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO fault_scenarios
               (scenario_id, fault_type, ground_truth_service, t_inject, status, params)
               VALUES (%s, %s, %s, now(), 'running', %s)""",
            (scenario_id, fault_type, service, json.dumps(params)),
        )


def mark_recovered(scenario_id: str) -> None:
    db = ensure_db_connection()
    with db.cursor() as cur:
        cur.execute(
            "UPDATE fault_scenarios SET status = 'recovered', t_recovered = now() WHERE scenario_id = %s",
            (scenario_id,),
        )


def mark_failed(scenario_id: str, error: str) -> None:
    db = ensure_db_connection()
    with db.cursor() as cur:
        cur.execute(
            "UPDATE fault_scenarios SET status = 'failed', t_recovered = now(), "
            "params = params || %s::jsonb WHERE scenario_id = %s",
            (json.dumps({"error": error}), scenario_id),
        )


def new_scenario_id(fault_type: str) -> str:
    return f"scn-{fault_type.replace('_', '-')}-{int(time.time())}"


def offered_rps() -> float | None:
    """Load being driven through the testbed right now, or None if unknown.

    A fault injected into an idle testbed moves no metric, so nothing can
    detect or diagnose it (issue #6) - and from `fault_scenarios` alone that
    looks identical to a detector that simply missed it. Recording the
    offered rate with each scenario makes an idle run visible in the ground
    truth instead of silently deflating the evaluation numbers.
    """
    try:
        resp = requests.get(f"{LOAD_GENERATOR_URL}/stats", timeout=3)
        resp.raise_for_status()
        stats = resp.json()
        # recent_rps, not the lifetime average: a generator that ran for an
        # hour and then stalled still has a healthy-looking average.
        return float(stats.get("recent_rps", stats["achieved_rps"]))
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        log.warning("could not read load-generator stats: %s", exc)
        return None


# ---------------------------------------------------------------------
# Fault implementations
# ---------------------------------------------------------------------

def run_bad_deploy_latency(service: str, duration_s: int, cpu_limit: float):
    try:
        requests.post(
            f"{DEPLOY_EMITTER_URL}/deploys",
            json={"service": service, "config_diff": "perf regression: inefficient loop introduced"},
            timeout=5,
        )
    except requests.RequestException as exc:
        log.warning("could not record companion deploy event: %s", exc)

    container = get_container(service)
    period = 100000
    quota = max(int(period * cpu_limit), 1000)
    log.info("throttling %s to %.0f%% CPU for %ss", service, cpu_limit * 100, duration_s)
    container.update(cpu_period=period, cpu_quota=quota)
    try:
        time.sleep(duration_s)
    finally:
        container.update(cpu_period=period, cpu_quota=-1)
        log.info("restored %s CPU", service)


def run_service_crash(service: str, duration_s: int):
    container = get_container(service)
    log.info("stopping %s for %ss", service, duration_s)
    container.stop(timeout=5)
    try:
        time.sleep(duration_s)
    finally:
        container.start()
        log.info("restarted %s", service)


def run_db_pool_saturation(service: str, duration_s: int, connections: int):
    if service != "catalogue":
        raise ValueError("db_pool_saturation currently only supports service=catalogue (MySQL)")

    held = []
    try:
        log.info("opening %d held connections against catalogue-db", connections)
        for _ in range(connections):
            try:
                c = pymysql.connect(host="catalogue-db", user="root", password="Abcd1234#",
                                     database="socksdb", connect_timeout=3)
                held.append(c)
            except pymysql.MySQLError as exc:
                log.warning("stopped opening new connections early: %s", exc)
                break
        time.sleep(duration_s)
    finally:
        for c in held:
            try:
                c.close()
            except pymysql.MySQLError:
                pass
        log.info("released %d held connections", len(held))


def execute(scenario_id: str, fault_type: str, service: str, params: dict):
    try:
        if fault_type == "bad_deploy_latency":
            run_bad_deploy_latency(service, params["duration_s"], params["cpu_limit"])
        elif fault_type == "service_crash":
            run_service_crash(service, params["duration_s"])
        elif fault_type == "db_pool_saturation":
            run_db_pool_saturation(service, params["duration_s"], params["connections"])
        mark_recovered(scenario_id)
        log.info("scenario %s recovered", scenario_id)
    except Exception as exc:
        log.exception("scenario %s failed", scenario_id)
        mark_failed(scenario_id, str(exc))


# ---------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.get("/fault-types")
def fault_types():
    return jsonify(sorted(FAULT_TYPES))


@app.post("/faults")
def post_fault():
    body = request.get_json(force=True, silent=True) or {}
    fault_type = body.get("fault_type")
    service = body.get("service")

    if fault_type not in FAULT_TYPES:
        return jsonify({"error": f"fault_type must be one of {sorted(FAULT_TYPES)}"}), 400
    if service not in KNOWN_SERVICES:
        return jsonify({"error": f"service must be one of {KNOWN_SERVICES}"}), 400

    duration_s = min(int(body.get("duration_s", 30)), MAX_DURATION_SECONDS)
    params = {"duration_s": duration_s}
    if fault_type == "bad_deploy_latency":
        # cpu_limit is a fraction of ONE core, and a quota only bites when it
        # is below what the service actually uses. These are small Go/Node
        # services: under 5 req/s of standing load, catalogue idles at ~0.17%
        # of a core, so the old 0.05 (5%) left it ~30x more CPU than it
        # needed and the "fault" changed nothing - measured, p95 flat at
        # 4.8ms through a 90s throttle. 0.002 (0.2%) took the same service
        # from 4.8ms to 160ms p95 within 30s at unchanged request rate.
        # Raise it for a heavier service (front-end idles near 1.8%).
        params["cpu_limit"] = float(body.get("cpu_limit", 0.002))
    if fault_type == "db_pool_saturation":
        params["connections"] = min(int(body.get("connections", 50)), MAX_CONNECTIONS)

    # Ground truth for the evaluation runner: how much traffic the testbed
    # was actually serving when this fault landed.
    rps = offered_rps()
    params["offered_rps_at_inject"] = rps

    scenario_id = new_scenario_id(fault_type)
    try:
        record_scenario(scenario_id, fault_type, service, params)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    threading.Thread(target=execute, args=(scenario_id, fault_type, service, params), daemon=True).start()

    response = {
        "scenario_id": scenario_id, "fault_type": fault_type,
        "ground_truth_service": service, "t_inject": now_iso(),
        "params": params, "status": "running",
    }
    if rps is None:
        response["warning"] = ("could not reach the load generator - if no traffic is running, "
                               "this fault will change no metric and nothing can detect it")
    elif rps < MIN_USEFUL_RPS:
        response["warning"] = (f"testbed is nearly idle ({rps} req/s offered) - "
                               "this fault may change no metric")
    return jsonify(response), 202


@app.get("/faults")
def get_faults():
    limit = min(int(request.args.get("limit", 20)), 200)
    db = ensure_db_connection()
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM fault_scenarios ORDER BY t_inject DESC LIMIT %s", (limit,))
        rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r["t_inject"] = r["t_inject"].isoformat()
        if r["t_recovered"]:
            r["t_recovered"] = r["t_recovered"].isoformat()
    return jsonify(rows)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)
