"""
Deploy event emitter (Phase 5). The single most important input to M3's
root-cause correlation - without a clean deploy log, the reasoning layer
has nothing to correlate anomalies against.

Two ways events get created:
  - POST /deploys - real trigger point. M2's fault-injection harness
    (Phase 8) will call this to record "a bad deploy just happened"
    before injecting the corresponding fault.
  - A background loop that fabricates an ordinary version-bump deploy to
    a random service every SIMULATE_INTERVAL_SECONDS, so there's a
    realistic, growing deploy history from day one instead of an empty
    table until someone manually calls the API.

Every event is both persisted to the `deploys` table (source of truth,
queryable by service/time) and published onto the deploys.events Kafka
topic (CONTRACTS.md shape), so M3 can consume it either way.
"""

import os
import json
import random
import logging
import threading
import time
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
from flask import Flask, request, jsonify
from kafka import KafkaProducer
from kafka.errors import NoBrokersAvailable

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("deploy-emitter")

PG_HOST = os.environ.get("PG_HOST", "timescaledb")
PG_PORT = os.environ.get("PG_PORT", "5432")
PG_DB = os.environ.get("PG_DB", "metrics")
PG_USER = os.environ.get("PG_USER", "postgres")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "Abcd1234#")

KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "kafka:9092")
TOPIC = "deploys.events"

SIMULATE_INTERVAL_SECONDS = float(os.environ.get("SIMULATE_INTERVAL_SECONDS", "120"))
KNOWN_SERVICES = ["front-end", "catalogue", "payment", "user", "carts", "orders", "shipping"]

CONFIG_DIFF_SAMPLES = [
    "bump base image version",
    "increase JVM heap limit",
    "tune DB connection pool size",
    "update dependency versions",
    "adjust upstream timeout config",
    "roll out feature flag",
    "adjust log level",
]

INSERT_SQL = """
    INSERT INTO deploys (deploy_id, service, version, commit_sha, config_diff, time)
    VALUES (
        'dep-' || to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD') || '-' || lpad(nextval('deploy_id_seq')::text, 4, '0'),
        %(service)s, %(version)s, %(commit_sha)s, %(config_diff)s, now()
    )
    RETURNING deploy_id, service, version, commit_sha, config_diff, time;
"""


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


def connect_kafka() -> KafkaProducer:
    while True:
        try:
            return KafkaProducer(
                bootstrap_servers=KAFKA_BOOTSTRAP,
                value_serializer=lambda v: json.dumps(v, default=str).encode("utf-8"),
                key_serializer=lambda k: k.encode("utf-8") if k else None,
            )
        except NoBrokersAvailable:
            log.warning("kafka not reachable yet, retrying in 3s")
            time.sleep(3)


conn = connect_postgres()
producer = connect_kafka()
app = Flask(__name__)


def next_version(service: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT version FROM deploys WHERE service = %s ORDER BY time DESC LIMIT 1",
            (service,),
        )
        row = cur.fetchone()
    if not row:
        return "1.0.0"
    parts = row[0].split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return "1.0.0"
    major, minor, patch = (int(p) for p in parts)
    return f"{major}.{minor}.{patch + 1}"


def random_commit_sha() -> str:
    return os.urandom(4).hex()[:7]


def create_deploy(service: str, version: str | None, commit_sha: str | None,
                   config_diff: str | None) -> dict:
    if service not in KNOWN_SERVICES:
        raise ValueError(f"unknown service {service!r}, expected one of {KNOWN_SERVICES}")

    version = version or next_version(service)
    commit_sha = commit_sha or random_commit_sha()
    config_diff = config_diff or f"{random.choice(CONFIG_DIFF_SAMPLES)} for {service}"

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(INSERT_SQL, {
            "service": service, "version": version,
            "commit_sha": commit_sha, "config_diff": config_diff,
        })
        record = dict(cur.fetchone())

    record["time"] = record["time"].astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    # CONTRACTS.md shape for deploys.events
    kafka_record = {
        "deploy_id": record["deploy_id"],
        "service": record["service"],
        "version": record["version"],
        "commit_sha": record["commit_sha"],
        "config_diff": record["config_diff"],
        "timestamp": record["time"],
    }
    producer.send(TOPIC, key=service, value=kafka_record)
    producer.flush()

    log.info("deploy recorded: %s", record)
    return record


@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.post("/deploys")
def post_deploy():
    body = request.get_json(force=True, silent=True) or {}
    service = body.get("service")
    if not service:
        return jsonify({"error": "service is required"}), 400
    try:
        record = create_deploy(
            service=service,
            version=body.get("version"),
            commit_sha=body.get("commit_sha"),
            config_diff=body.get("config_diff"),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(record), 201


@app.get("/deploys")
def get_deploys():
    service = request.args.get("service")
    limit = min(int(request.args.get("limit", 20)), 200)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if service:
            cur.execute(
                "SELECT * FROM deploys WHERE service = %s ORDER BY time DESC LIMIT %s",
                (service, limit),
            )
        else:
            cur.execute("SELECT * FROM deploys ORDER BY time DESC LIMIT %s", (limit,))
        rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r["time"] = r["time"].isoformat()
    return jsonify(rows)


def simulate_loop():
    log.info("simulating an ordinary deploy every %ss", SIMULATE_INTERVAL_SECONDS)
    while True:
        time.sleep(SIMULATE_INTERVAL_SECONDS)
        service = random.choice(KNOWN_SERVICES)
        try:
            create_deploy(service, version=None, commit_sha=None, config_diff=None)
        except Exception:
            log.exception("simulated deploy failed")


if __name__ == "__main__":
    threading.Thread(target=simulate_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)
