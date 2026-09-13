"""Background consumer: anomalies.detected -> anomalies table.

M2 publishes anomalies but nothing stores them, and POST /analyze only receives an id, so
this service keeps its own copy (PLAN.md section 1, "Anomaly lookup"). Kafka offsets are
committed only after the database commit, so a crash re-reads a few events, which
save_anomaly reports as duplicates. Retry-until-up follows services/metrics-sink/main.py.
"""

import json
import logging
import threading
from collections import Counter

import psycopg2
from kafka import KafkaConsumer
from kafka.errors import NoBrokersAvailable
from pydantic import ValidationError

from app.db import connect, save_anomaly
from app.models import AnomalyEvent
from app.settings import Settings

log = logging.getLogger("diagnosis-service.consumer")

TOPIC = "anomalies.detected"
GROUP_ID = "diagnosis-service"
RETRY_SECONDS = 3


def handle_message(cur, value: bytes | None) -> str:
    """Validate one Kafka message value and store it.

    Returns "inserted", "duplicate", "collision" or "invalid". A malformed event is logged
    and skipped, not raised: re-reading it would fail the same way, and stalling the
    partition on it would stop every anomaly behind it.
    """
    try:
        raw = json.loads(value)
        event = AnomalyEvent.model_validate(raw)
    except (TypeError, ValueError, ValidationError) as exc:
        log.warning("skipping malformed anomaly %r: %s", value[:500] if value else value, exc)
        return "invalid"

    result = save_anomaly(cur, event, raw)
    if result == "inserted":
        log.info(
            "stored anomaly %s services=%s metrics=%s severity=%s",
            event.anomaly_id,
            event.services,
            event.metrics,
            event.severity,
        )
    elif result == "collision":
        log.warning(
            "anomaly_id collision on %s: kept the stored event, dropped %r (issue #4)",
            event.anomaly_id,
            raw,
        )
    return result


class AnomalyConsumer:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.state = "stopped"  # stopped | connecting | running
        self.counts: Counter[str] = Counter()

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="anomaly-consumer", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.state = "connecting"
            try:
                self._consume()
            except Exception:
                log.exception("anomaly consumer failed; reconnecting in %ss", RETRY_SECONDS)
                self._stop.wait(RETRY_SECONDS)
        self.state = "stopped"

    def _consume(self) -> None:
        consumer = self._connect_kafka()
        if consumer is None:
            return
        try:
            conn = self._connect_postgres()
            if conn is None:
                return
            try:
                self.state = "running"
                log.info("consuming %s from %s", TOPIC, self._settings.kafka_bootstrap)
                while not self._stop.is_set():
                    batches = consumer.poll(timeout_ms=1000)
                    if not batches:
                        continue
                    results: Counter[str] = Counter()
                    with conn, conn.cursor() as cur:
                        for messages in batches.values():
                            for message in messages:
                                results[handle_message(cur, message.value)] += 1
                    consumer.commit()
                    self.counts.update(results)
            finally:
                conn.close()
        finally:
            consumer.close()

    def _connect_kafka(self) -> KafkaConsumer | None:
        while not self._stop.is_set():
            try:
                return KafkaConsumer(
                    TOPIC,
                    bootstrap_servers=self._settings.kafka_bootstrap,
                    group_id=GROUP_ID,
                    enable_auto_commit=False,
                    # First start with no committed offset backfills what is still on the topic.
                    auto_offset_reset="earliest",
                )
            except NoBrokersAvailable:
                log.warning("kafka not reachable yet, retrying in %ss", RETRY_SECONDS)
                self._stop.wait(RETRY_SECONDS)
        return None

    def _connect_postgres(self):
        while not self._stop.is_set():
            try:
                return connect(self._settings)
            except psycopg2.OperationalError as exc:
                log.warning("timescaledb not reachable yet (%s), retrying in %ss", exc, RETRY_SECONDS)
                self._stop.wait(RETRY_SECONDS)
        return None
