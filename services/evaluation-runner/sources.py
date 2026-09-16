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

from scoring import DetectedEvent, Scenario, parse_ts, to_detected_event

log = logging.getLogger("evaluation-runner")

PG_HOST = os.environ.get("PG_HOST", "timescaledb")
PG_PORT = os.environ.get("PG_PORT", "5432")
PG_DB = os.environ.get("PG_DB", "metrics")
PG_USER = os.environ.get("PG_USER", "postgres")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "Abcd1234#")

FAULT_INJECTOR_URL = os.environ.get("FAULT_INJECTOR_URL", "http://fault-injector:5001")
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
