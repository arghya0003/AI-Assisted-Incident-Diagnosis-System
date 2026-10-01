"""
I/O boundaries for the evaluation harness: the fault injector, the
`anomalies.detected` topic, and TimescaleDB.

Everything here returns the plain dataclasses from `scoring.py`, so the
scoring logic never touches a socket and stays unit-testable.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime

import psycopg2
import psycopg2.extras
import requests

from attribution import HypothesisRow
from scoring import DetectedEvent, Scenario, parse_ts, to_detected_event

log = logging.getLogger("evaluation-runner")

PG_HOST = os.environ.get("PG_HOST", "timescaledb")
PG_PORT = os.environ.get("PG_PORT", "5432")
PG_DB = os.environ.get("PG_DB", "metrics")
PG_USER = os.environ.get("PG_USER", "postgres")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "Abcd1234#")

FAULT_INJECTOR_URL = os.environ.get("FAULT_INJECTOR_URL", "http://fault-injector:5001")
DIAGNOSIS_URL = os.environ.get("DIAGNOSIS_URL", "http://diagnosis-service:8000")
KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "kafka:9092")
ANOMALY_TOPIC = "anomalies.detected"


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


def supported_fault_types() -> set[str] | None:
    """Ask the injector which fault types it can produce.

    Returns None when the injector cannot be reached or answers oddly. That
    is deliberately distinct from an empty set: "I could not ask" must not be
    read as "it supports nothing", or a transient network blip would skip an
    entire run's worth of scenarios and report a 0% detection rate.
    """
    try:
        response = requests.get(f"{FAULT_INJECTOR_URL}/fault-types", timeout=5)
        response.raise_for_status()
        types = response.json()
    except Exception as exc:
        log.warning("could not read supported fault types (%s); attempting all scenarios", exc)
        return None
    if not isinstance(types, list) or not all(isinstance(t, str) for t in types):
        log.warning("unexpected /fault-types payload %r; attempting all scenarios", types)
        return None
    return set(types)


def inject_fault(spec) -> str:
    """Start one fault and return its scenario_id."""
    response = requests.post(
        f"{FAULT_INJECTOR_URL}/faults", json=spec.request_body(), timeout=10
    )
    response.raise_for_status()
    return response.json()["scenario_id"]


def load_scenarios(conn, scenario_ids: list[str] | None = None,
                   since: datetime | None = None) -> list[Scenario]:
    """Read ground truth back from `fault_scenarios`.

    The database is the authority on `t_inject`, not the runner's own clock:
    the injector records the timestamp at the moment the fault actually
    started, and using anything else would quietly bias every latency number.
    """
    query = "SELECT scenario_id, fault_type, ground_truth_service, t_inject, t_recovered, status FROM fault_scenarios"
    params: list = []
    if scenario_ids:
        query += " WHERE scenario_id = ANY(%s)"
        params.append(scenario_ids)
    elif since:
        query += " WHERE t_inject >= %s"
        params.append(since)
    query += " ORDER BY t_inject"

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        rows = cur.fetchall()

    return [
        Scenario(
            scenario_id=r["scenario_id"],
            fault_type=r["fault_type"],
            ground_truth_service=r["ground_truth_service"],
            t_inject=parse_ts(r["t_inject"]),
            t_recovered=parse_ts(r["t_recovered"]) if r["t_recovered"] else None,
            status=r["status"],
        )
        for r in rows
    ]


def load_metric_samples(conn, start: datetime, end: datetime,
                        exclude_metrics: set[str] | None = None) -> list[tuple]:
    """Raw samples in time order, for offline replay through a detector.

    Reads the raw `metrics` hypertable rather than the 1-minute rollup:
    replaying a rollup would hand every detector a pre-smoothed series and
    make the comparison meaningless.
    """
    exclude = exclude_metrics or set()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT time, service, metric, value
               FROM metrics
               WHERE time BETWEEN %s AND %s
               ORDER BY time ASC""",
            (start, end),
        )
        return [
            (parse_ts(t), service, metric, float(value))
            for t, service, metric, value in cur.fetchall()
            if metric not in exclude
        ]


# Process-level metrics every container reports whether or not it is serving
# anything. A service emitting only these is idle, not healthy — throttling it
# changes nothing anyone can measure.
PROCESS_METRICS = {"cpu_rate", "memory_bytes"}


def had_request_telemetry(conn, service: str, start: datetime, end: datetime) -> bool:
    """Did this service report anything beyond process-level metrics?

    Sock Shop runs without a load generator, so several services serve no
    traffic at all; their request histograms are empty, `histogram_quantile`
    returns NaN and metrics-bridge drops the sample. A latency fault on such a
    service is undetectable by construction, and scoring it as a detector miss
    would be measuring the testbed while blaming the detector.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT count(DISTINCT metric) FROM metrics
               WHERE service = %s AND time BETWEEN %s AND %s
                 AND metric <> ALL(%s)""",
            (service, start, end, list(PROCESS_METRICS)),
        )
        return cur.fetchone()[0] > 0


def load_deploys(conn, start: datetime, end: datetime) -> list[tuple[str, str, datetime]]:
    """Deploy history, so replay applies the same deploy-window policy as live."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT deploy_id, service, time FROM deploys WHERE time BETWEEN %s AND %s ORDER BY time",
            (start, end),
        )
        return [(deploy_id, service, parse_ts(t)) for deploy_id, service, t in cur.fetchall()]


class AnomalyCollector:
    """Collects `anomalies.detected` in the background during a live run."""

    def __init__(self, bootstrap: str = KAFKA_BOOTSTRAP):
        self.bootstrap = bootstrap
        self.events: list[DetectedEvent] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        # Block until the consumer is actually subscribed, otherwise the
        # first fault can be injected before anything is listening and its
        # detection is silently lost.
        if not self._ready.wait(timeout=60):
            raise RuntimeError("anomaly consumer did not become ready within 60s")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def snapshot(self) -> list[DetectedEvent]:
        with self._lock:
            return list(self.events)

    def _run(self) -> None:
        from kafka import KafkaConsumer

        consumer = KafkaConsumer(
            ANOMALY_TOPIC,
            bootstrap_servers=self.bootstrap,
            group_id=None,
            value_deserializer=lambda v: json.loads(v.decode("utf-8")),
            enable_auto_commit=False,
            auto_offset_reset="latest",
            consumer_timeout_ms=1000,
        )
        consumer.poll(timeout_ms=5000)  # force partition assignment
        self._ready.set()

        while not self._stop.is_set():
            for msg in consumer:
                event = to_detected_event(msg.value)
                if event is None:
                    continue
                with self._lock:
                    self.events.append(event)
                log.info("observed %s services=%s", event.anomaly_id, list(event.services))

        consumer.close()


# --------------------------------------------------------------- attribution

def find_anomaly_for_scenario(conn, service: str, start: datetime, end: datetime) -> str | None:
    """The first anomaly naming `service` inside the fault window.

    Read from the `anomalies` table rather than the Kafka topic, so a run can
    be scored for attribution long after the 24h retention has passed — which
    is the whole reason the detector writes there.

    The earliest matching anomaly is the one the ranker should be asked about:
    a later one in the same window is the same incident re-alerting after the
    cooldown lapsed, and diagnosing it would score the same fault twice.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT anomaly_id FROM anomalies
            WHERE %s = ANY(services) AND t_detected BETWEEN %s AND %s
            ORDER BY t_detected ASC LIMIT 1
            """,
            (service, start, end),
        )
        row = cur.fetchone()
    return row[0] if row else None


def request_diagnosis(anomaly_id: str, timeout: int = 120) -> bool:
    """Ask M3's pipeline to diagnose one anomaly.

    Returns whether the call succeeded. A failure is logged and reported
    rather than raised: one unreachable diagnosis should not throw away the
    scoring for every other scenario in the run.

    The generous timeout is not defensive padding — the pipeline's own
    recorded latencies run to several seconds per analysis, and an LLM-backed
    mode can be far slower than the deterministic fallback.
    """
    try:
        response = requests.post(
            f"{DIAGNOSIS_URL}/analyze", json={"anomaly_id": anomaly_id}, timeout=timeout
        )
        response.raise_for_status()
        return True
    except Exception as exc:
        log.warning("diagnosis failed for %s: %s", anomaly_id, exc)
        return False


def load_hypotheses(conn, anomaly_id: str) -> list[HypothesisRow]:
    """The most recent analysis's hypotheses for one anomaly.

    Read from the database rather than the `POST /analyze` response. The
    response now carries `service` too (issue #37), so either would work - but
    the `attribute` subcommand also has to score runs with `--no-analyze`,
    where there is no response to read. One path that always works beats two
    that each cover half the cases.

    Scoped to the latest `analysis_id` so re-diagnosing an anomaly does not
    blend two runs' rankings into one list.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT rank, cause, confidence, evidence_ids, service
            FROM hypotheses
            WHERE anomaly_id = %s
              AND analysis_id IS NOT DISTINCT FROM (
                  SELECT analysis_id FROM hypotheses
                  WHERE anomaly_id = %s
                  ORDER BY created_at DESC LIMIT 1
              )
            ORDER BY rank ASC
            """,
            (anomaly_id, anomaly_id),
        )
        return [
            HypothesisRow(
                rank=r["rank"],
                cause=r["cause"],
                confidence=r["confidence"],
                evidence_ids=tuple(r["evidence_ids"] or ()),
                service=r["service"],
            )
            for r in cur.fetchall()
        ]


def resolve_evidence_ids(conn, evidence_ids: list[str]) -> set[str]:
    """Which cited ids point at a record that actually exists.

    Three id shapes are accepted because the pipeline cites all of them: the
    `evidence` table's own ids, and the raw `anomalies` / `deploys` source ids
    it uses instead. An id in none of them is a citation of something that
    does not exist, which is exactly what evidence validity is meant to catch.
    """
    if not evidence_ids:
        return set()
    unique = list({e for e in evidence_ids})
    found: set[str] = set()
    queries = (
        "SELECT evidence_id FROM evidence WHERE evidence_id = ANY(%s)",
        "SELECT anomaly_id FROM anomalies WHERE anomaly_id = ANY(%s)",
        "SELECT deploy_id FROM deploys WHERE deploy_id = ANY(%s)",
    )
    for query in queries:
        try:
            with conn.cursor() as cur:
                cur.execute(query, (unique,))
                found.update(row[0] for row in cur.fetchall())
        except psycopg2.Error as exc:
            # A missing table is M3's or M1's schema drifting, not a reason to
            # report every citation as invalid.
            log.warning("could not check evidence ids (%s): %s", query.split()[3], exc)
            conn.rollback()
    return found
