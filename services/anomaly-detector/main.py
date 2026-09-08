"""
Anomaly detection (M2, Phase 1). Consumes metrics.raw, runs two independent
per-(service, metric) detectors side by side - an adaptive EWMA z-score
detector and a frozen static-threshold detector - so the evaluation runner
(services/eval-runner/) can score EWMA against a real comparison baseline
instead of just asserting it works.

Design decisions, in one place rather than scattered as inline comments:

- Grouping/dedup: a single fault typically trips several (service, metric)
  pairs within the same few seconds (metrics-bridge scrapes every 5s - see
  CONTRACTS.md). Raw per-metric detections are buffered and flushed every
  FLUSH_INTERVAL_SECONDS into one anomalies.detected event per flush, with
  services[]/metrics[] deduped - not fifteen separate alerts. This matches
  the plan's explicit ask ("emit one anomaly event with a member list").
- Deploy-window suppression: a real deploy can cause a brief, benign metric
  blip (connection warm-up, JIT warm-up, cache misses) that isn't the kind
  of anomaly this system should alert a human about. Detections for a
  service are suppressed for DEPLOY_SETTLE_WINDOW_SECONDS after that
  service's most recent deploy (tracked by consuming deploys.events). This
  is a settle window, not a baseline reset: a fault that starts at deploy
  time (like the bad_deploy_latency fault scenario) and persists past the
  settle window still fires - it costs that fault type a bit of extra
  detection latency, which the evaluation report should show honestly
  rather than hide.
- Seasonality suppression (day/week cyclic baselines) is explicitly NOT
  implemented: this testbed has no real diurnal/weekly traffic pattern to
  suppress against, and faking one would just be a made-up detector with
  nothing real to validate it. Documented gap, not a silent one - revisit
  if the testbed ever runs long enough to have real seasonality.
- Only the EWMA detector's grouped anomalies are published to
  anomalies.detected (the contract M4 builds against). Both detectors'
  grouped anomalies are persisted to the `anomalies` table
  (timescaledb/init/005_anomalies.sql), tagged by `detector`, which is what
  the evaluation runner reads to compare them.
"""

import json
import logging
import math
import os
import random
import statistics
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import psycopg2
from kafka import KafkaConsumer, KafkaProducer
from kafka.errors import KafkaConnectionError

try:
    from kafka.errors import NoBrokersAvailable
except ImportError:  # kafka-python >= 3.0 removes this symbol
    NoBrokersAvailable = KafkaConnectionError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anomaly-detector")

KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "kafka:9092")
METRICS_TOPIC = "metrics.raw"
DEPLOYS_TOPIC = "deploys.events"
ANOMALIES_TOPIC = "anomalies.detected"
GROUP_ID = "anomaly-detector"

PG_HOST = os.environ.get("PG_HOST", "timescaledb")
PG_PORT = os.environ.get("PG_PORT", "5432")
PG_DB = os.environ.get("PG_DB", "metrics")
PG_USER = os.environ.get("PG_USER", "postgres")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "Abcd1234#")

FLUSH_INTERVAL_SECONDS = float(os.environ.get("FLUSH_INTERVAL_SECONDS", "5"))
DEPLOY_SETTLE_WINDOW_SECONDS = float(os.environ.get("DEPLOY_SETTLE_WINDOW_SECONDS", "8"))

SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class EWMAAnomalyDetector:
    """Adaptive baseline: exponentially-weighted mean/variance, alpha-tuned
    to keep tracking the "normal" level of a metric as it drifts, flagging
    samples that are z_threshold standard deviations away from it."""

    alpha: float = 0.2
    z_threshold: float = 3.0
    warmup: int = 10
    service: str = "catalogue"
    metric: str = "latency_p99_ms"
    _ewma: float | None = None
    _ewmvar: float | None = None
    _count: int = 0

    def _current_mean(self) -> float:
        return self._ewma if self._ewma is not None else 0.0

    def _current_std(self) -> float:
        if self._ewmvar is None:
            return 0.0
        return math.sqrt(max(self._ewmvar, 0.0))

    def update(self, value: float):
        if self._count == 0:
            self._ewma = value
            self._ewmvar = 0.0
            self._count = 1
            return None

        prev_mean = self._current_mean()
        prev_std = self._current_std()

        if self._count < self.warmup:
            prev = self._ewma
            self._ewma = self.alpha * value + (1 - self.alpha) * prev
            self._ewmvar = self.alpha * (value - prev) ** 2 + (1 - self.alpha) * (self._ewmvar or 0.0)
            self._count += 1
            return None

        baseline_std = max(prev_std, 0.5)
        z = abs(value - prev_mean) / baseline_std

        # update the baseline after deciding whether the sample is anomalous,
        # so a spike doesn't immediately drag the baseline toward itself
        prev = self._ewma
        self._ewma = self.alpha * value + (1 - self.alpha) * prev
        self._ewmvar = self.alpha * (value - prev) ** 2 + (1 - self.alpha) * (self._ewmvar or 0.0)
        self._count += 1

        if z >= self.z_threshold:
            severity = "high" if z >= 4.5 else "medium"
            return {
                "service": self.service,
                "metric": self.metric,
                "severity": severity,
                "t_onset": now_iso(),
                "value": value,
                "baseline": prev_mean,
                "z_score": z,
            }
        return None


@dataclass
class StaticThresholdDetector:
    """Comparison baseline (per the plan: "at least one comparison baseline
    ... so the evaluation can show that EWMA actually earned its place").
    Learns a plain mean/stdev over the first `warmup` samples, then freezes
    threshold = mean + k*stdev forever - deliberately naive, so it never
    adapts to legitimate drift (and will false-alarm on it) and never
    tightens back up after a real regression subsides."""

    warmup: int = 10
    k: float = 3.0
    service: str = "catalogue"
    metric: str = "latency_p99_ms"
    _samples: list = field(default_factory=list)
    _baseline_mean: float | None = None
    _threshold: float | None = None

    def update(self, value: float):
        if self._threshold is None:
            self._samples.append(value)
            if len(self._samples) >= self.warmup:
                mean = statistics.fmean(self._samples)
                std = statistics.pstdev(self._samples)
                self._baseline_mean = mean
                self._threshold = mean + self.k * max(std, 0.5)
            return None

        if value > self._threshold:
            severity = "high" if value >= self._threshold * 1.5 else "medium"
            return {
                "service": self.service,
                "metric": self.metric,
                "severity": severity,
                "t_onset": now_iso(),
                "value": value,
                "baseline": self._baseline_mean,
                "threshold": self._threshold,
            }
        return None


def is_within_deploy_settle_window(last_deploy_ts: float | None, now: float) -> bool:
    if last_deploy_ts is None:
        return False
    return (now - last_deploy_ts) < DEPLOY_SETTLE_WINDOW_SECONDS


def group_detections(detections: list[dict], id_prefix: str) -> dict:
    """Merge a batch of raw per-(service, metric) detections from one flush
    window into a single grouped anomaly record. Pure function - no I/O -
    so it's directly unit-testable without Kafka/Postgres running."""
    if not detections:
        raise ValueError("group_detections requires at least one detection")

    services = sorted({d["service"] for d in detections})
    metrics = sorted({d["metric"] for d in detections})
    severity = max((d["severity"] for d in detections), key=lambda s: SEVERITY_RANK.get(s, 0))
    onsets = [d["t_onset"] for d in detections if d.get("t_onset")]
    t_onset = min(onsets) if onsets else now_iso()
    t_detected = now_iso()

    return {
        "anomaly_id": f"anom-{id_prefix}-{int(time.time() * 1000)}-{random.randint(100, 999)}",
        "services": services,
        "metrics": metrics,
        "severity": severity,
        "t_detected": t_detected,
        "t_onset": t_onset,
        "evidence_window": {"start": t_onset, "end": t_detected},
    }


def connect_kafka_consumer(topic: str, group_id: str, offset_reset: str = "earliest") -> KafkaConsumer:
    while True:
        try:
            return KafkaConsumer(
                topic,
                bootstrap_servers=KAFKA_BOOTSTRAP,
                group_id=group_id,
                value_deserializer=lambda v: json.loads(v.decode("utf-8")),
                key_deserializer=lambda k: k.decode("utf-8") if k else None,
                enable_auto_commit=True,
                auto_offset_reset=offset_reset,
                consumer_timeout_ms=1000,
            )
        except NoBrokersAvailable:
            log.warning("kafka not reachable yet (%s), retrying in 3s", topic)
            time.sleep(3)


def connect_kafka_producer() -> KafkaProducer:
    while True:
        try:
            return KafkaProducer(
                bootstrap_servers=KAFKA_BOOTSTRAP,
                value_serializer=lambda v: json.dumps(v).encode("utf-8"),
                key_serializer=lambda k: k.encode("utf-8") if k else None,
            )
        except NoBrokersAvailable:
            log.warning("kafka producer not reachable yet, retrying in 3s")
            time.sleep(3)


def connect_postgres():
    while True:
        try:
            conn = psycopg2.connect(host=PG_HOST, port=PG_PORT, dbname=PG_DB, user=PG_USER, password=PG_PASSWORD)
            conn.autocommit = True
            return conn
        except psycopg2.OperationalError as exc:
            log.warning("timescaledb not reachable yet (%s), retrying in 3s", exc)
            time.sleep(3)


def persist_anomaly(conn, detector: str, grouped: dict, raw_detections: list[dict]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO anomalies
               (anomaly_id, detector, services, metrics, severity, t_onset, t_detected,
                evidence_window_start, evidence_window_end, detail)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (anomaly_id) DO NOTHING""",
            (
                grouped["anomaly_id"], detector, grouped["services"], grouped["metrics"],
                grouped["severity"], grouped["t_onset"], grouped["t_detected"],
                grouped["evidence_window"]["start"], grouped["evidence_window"]["end"],
                json.dumps(raw_detections),
            ),
        )


def publish_anomaly(producer: KafkaProducer, grouped: dict) -> None:
    producer.send(ANOMALIES_TOPIC, key=grouped["services"][0], value={
        "anomaly_id": grouped["anomaly_id"],
        "services": grouped["services"],
        "metrics": grouped["metrics"],
        "severity": grouped["severity"],
        "t_detected": grouped["t_detected"],
        "t_onset": grouped["t_onset"],
        "evidence_window": grouped["evidence_window"],
    })
    producer.flush()


def deploy_listener(last_deploy_ts: dict) -> None:
    """Watches deploys.events (own consumer group, `latest` offset - this
    only cares about deploys from now on) so detections can be suppressed
    during a service's post-deploy settle window."""
    consumer = connect_kafka_consumer(DEPLOYS_TOPIC, group_id="anomaly-detector-deploy-watch", offset_reset="latest")
    log.info("watching %s for deploy-window suppression", DEPLOYS_TOPIC)
    while True:
        for msg in consumer:
            service = msg.value.get("service")
            if service:
                last_deploy_ts[service] = time.time()
                log.info("deploy observed for %s; suppressing new detections for %ss", service, DEPLOY_SETTLE_WINDOW_SECONDS)


def run():
    consumer = connect_kafka_consumer(METRICS_TOPIC, group_id=GROUP_ID)
    producer = connect_kafka_producer()
    db_conn = connect_postgres()
    log.info("listening on %s, watching %s, publishing to %s", METRICS_TOPIC, DEPLOYS_TOPIC, ANOMALIES_TOPIC)

    last_deploy_ts: dict[str, float] = {}
    threading.Thread(target=deploy_listener, args=(last_deploy_ts,), daemon=True).start()

    ewma_detectors: dict[tuple[str, str], EWMAAnomalyDetector] = {}
    static_detectors: dict[tuple[str, str], StaticThresholdDetector] = {}
    pending: dict[str, list[dict]] = {"ewma": [], "static_threshold": []}
    last_flush = time.time()

    while True:
        for msg in consumer:
            record = msg.value
            service = record.get("service")
            metric = record.get("metric")
            value = record.get("value")
            if not service or not metric or value is None:
                continue

            key = (service, metric)
            if key not in ewma_detectors:
                ewma_detectors[key] = EWMAAnomalyDetector(service=service, metric=metric)
                static_detectors[key] = StaticThresholdDetector(service=service, metric=metric)

            ewma_hit = ewma_detectors[key].update(float(value))
            static_hit = static_detectors[key].update(float(value))

            if not (ewma_hit or static_hit):
                continue

            suppressed = is_within_deploy_settle_window(last_deploy_ts.get(service), time.time())
            timestamp = record.get("timestamp")

            if ewma_hit:
                if suppressed:
                    log.info("suppressed ewma detection for %s/%s (deploy settle window)", service, metric)
                else:
                    ewma_hit["t_onset"] = timestamp or ewma_hit["t_onset"]
                    pending["ewma"].append(ewma_hit)

            if static_hit and not suppressed:
                static_hit["t_onset"] = timestamp or static_hit["t_onset"]
                pending["static_threshold"].append(static_hit)

        now = time.time()
        if now - last_flush >= FLUSH_INTERVAL_SECONDS:
            for detector_name, items in pending.items():
                if items:
                    grouped = group_detections(items, id_prefix=detector_name.replace("_threshold", ""))
                    try:
                        persist_anomaly(db_conn, detector_name, grouped, items)
                    except psycopg2.Error:
                        log.exception("failed to persist anomaly, reconnecting")
                        db_conn = connect_postgres()
                    if detector_name == "ewma":
                        publish_anomaly(producer, grouped)
                    log.info("flushed %d %s detection(s) into anomaly %s (services=%s, severity=%s)",
                              len(items), detector_name, grouped["anomaly_id"], grouped["services"], grouped["severity"])
                pending[detector_name] = []
            last_flush = now

        time.sleep(0.5)


if __name__ == "__main__":
    run()
