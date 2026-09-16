"""
Anomaly detection service (M2).

Consumes `metrics.raw`, runs a per-(service, metric) drift detector, groups
correlated breaches into a single incident, and publishes to
`anomalies.detected` in the CONTRACTS.md shape. Each event is also recorded
in the `anomalies` table so it can be looked up by ID later (store.py).

The detector itself is swappable via the DETECTOR env var (ewma, zscore,
cusum, static) so the evaluation runner can measure one against another. See
detectors.py for why that swap-ability is load-bearing rather than decorative.
"""

import os
import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone

import psycopg2
from kafka import KafkaConsumer, KafkaProducer
from kafka.errors import KafkaConnectionError

from detectors import Detector, build_detector
from deploy_window import DeployWindowTracker, consume_deploys
from grouping import AnomalyGrouper, parse_ts
from staleness import StalenessMonitor
from store import AnomalyStore

try:
    from kafka.errors import NoBrokersAvailable
except ImportError:  # kafka-python >= 3.0 removes this symbol
    NoBrokersAvailable = KafkaConnectionError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anomaly-detector")

KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "kafka:9092")
INPUT_TOPIC = "metrics.raw"
OUTPUT_TOPIC = "anomalies.detected"
GROUP_ID = "anomaly-detector"

DETECTOR_KIND = os.environ.get("DETECTOR", "ewma")
REQUIRED_BREACHES = int(os.environ.get("REQUIRED_BREACHES", "2"))
WARMUP_SAMPLES = int(os.environ.get("WARMUP_SAMPLES", "10"))
GROUP_DELAY_SECONDS = float(os.environ.get("GROUP_DELAY_SECONDS", "15"))
COOLDOWN_SECONDS = float(os.environ.get("COOLDOWN_SECONDS", "120"))
# A signal this many times worse than the incident that muted its service
# breaks through the cooldown as a new incident. See grouping.py.
ESCALATION_FACTOR = float(os.environ.get("ESCALATION_FACTOR", "3"))
DEPLOY_WINDOW_SECONDS = float(os.environ.get("DEPLOY_WINDOW_SECONDS", "120"))
# Six missed 5s scrape cycles: long enough not to flap on a slow scrape,
# short enough to report an outage well inside the 60s latency target.
STALE_AFTER_SECONDS = float(os.environ.get("STALE_AFTER_SECONDS", "30"))

# Metrics with no meaningful "too high" reading. request_rate moves with
# ordinary traffic, so alerting on it produces noise, not incidents.
IGNORED_METRICS = {m for m in os.environ.get("IGNORED_METRICS", "request_rate").split(",") if m}

PG_HOST = os.environ.get("PG_HOST", "timescaledb")
PG_PORT = os.environ.get("PG_PORT", "5432")
PG_DB = os.environ.get("PG_DB", "metrics")
PG_USER = os.environ.get("PG_USER", "postgres")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "Abcd1234#")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def connect_kafka_consumer() -> KafkaConsumer:
    while True:
        try:
            return KafkaConsumer(
                INPUT_TOPIC,
                bootstrap_servers=KAFKA_BOOTSTRAP,
                group_id=GROUP_ID,
                value_deserializer=lambda v: json.loads(v.decode("utf-8")),
                key_deserializer=lambda k: k.decode("utf-8") if k else None,
                enable_auto_commit=False,
                # On a first start, skip the backlog rather than replaying up
                # to 24h of retained metrics and alerting on faults that are
                # long over. Committed offsets still resume normally.
                auto_offset_reset="latest",
                consumer_timeout_ms=1000,
            )
        except NoBrokersAvailable:
            log.warning("kafka not reachable yet, retrying in 3s")
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
    # One attempt with a short timeout, unlike the Kafka connections above:
    # AnomalyStore retries on the next event, and a slow database must not
    # stall the alerting loop.
    conn = psycopg2.connect(
        host=PG_HOST, port=PG_PORT, dbname=PG_DB,
        user=PG_USER, password=PG_PASSWORD, connect_timeout=3,
    )
    conn.autocommit = True
    return conn


def run():
    consumer = connect_kafka_consumer()
    producer = connect_kafka_producer()
    store = AnomalyStore(connect_postgres)

    tracker = DeployWindowTracker(window_seconds=DEPLOY_WINDOW_SECONDS)
    stop = threading.Event()
    threading.Thread(
        target=consume_deploys, args=(tracker, KAFKA_BOOTSTRAP, stop), daemon=True
    ).start()

    grouper = AnomalyGrouper(
        group_delay_seconds=GROUP_DELAY_SECONDS,
        cooldown_seconds=COOLDOWN_SECONDS,
        escalation_factor=ESCALATION_FACTOR,
        # anomaly_id is a primary key downstream (the anomalies table, M3,
        # M4's incidents), so a restart must never reuse one. See issue #4.
        id_namespace=uuid.uuid4().hex[:6],
    )
    staleness = StalenessMonitor(stale_after_seconds=STALE_AFTER_SECONDS)
    detectors: dict[tuple[str, str], Detector] = {}

    log.info(
        "detector=%s listening on %s, publishing grouped events to %s",
        DETECTOR_KIND, INPUT_TOPIC, OUTPUT_TOPIC,
    )

    while True:
        for msg in consumer:
            record = msg.value
            service = record.get("service")
            metric = record.get("metric")
            value = record.get("value")
            if not service or not metric or value is None:
                continue
            if metric in IGNORED_METRICS:
                continue

            timestamp = record.get("timestamp") or _iso(now_utc())
            staleness.observe(service, parse_ts(timestamp))
            key = (service, metric)
            if key not in detectors:
                detectors[key] = build_detector(
                    DETECTOR_KIND, service=service, metric=metric,
                    warmup=WARMUP_SAMPLES, required_breaches=REQUIRED_BREACHES,
                )

            sample_time = parse_ts(timestamp)
            needed, deploy_id = tracker.required_breaches(
                service, sample_time, REQUIRED_BREACHES
            )

            signal = detectors[key].update(float(value), timestamp, required_breaches=needed)
            if signal is None:
                continue

            signal.in_deploy_window = deploy_id is not None
            signal.deploy_id = deploy_id
            grouper.add(signal)
            log.info(
                "signal %s/%s value=%.4g baseline=%.4g score=%.2f%s",
                service, metric, signal.value, signal.baseline, signal.score,
                f" (deploy window {deploy_id})" if deploy_id else "",
            )

        # Driven by the clock, not by arriving samples: the whole point is to
        # notice a service that has stopped sending anything.
        tick = now_utc()
        for signal in staleness.check(tick):
            grouper.add(signal)
            log.info("%s has sent nothing for %.0fs", signal.service, signal.value)

        for event in grouper.flush(tick):
            producer.send(OUTPUT_TOPIC, key=event["services"][0], value=event)
            producer.flush()
            # After the publish, so the alert reaches M4 even if this fails.
            store.save(event)
            log.info(
                "emitted %s severity=%s services=%s metrics=%s (%d contributing signals)",
                event["anomaly_id"], event["severity"], event["services"],
                event["metrics"], len(event["contributors"]),
            )

        consumer.commit()


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


if __name__ == "__main__":
    run()
