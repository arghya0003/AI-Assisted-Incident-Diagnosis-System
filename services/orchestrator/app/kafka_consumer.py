"""Consumes `anomalies.detected` (M2 -> M4, CONTRACTS.md) and opens an incident for each one.
Runs in a background thread started by main.py's lifespan, matching the connection-retry
pattern services/anomaly-detector/main.py and services/deploy-emitter/main.py already use.
"""

import json
import logging
import threading

from kafka import KafkaConsumer
from kafka.errors import KafkaConnectionError
from pydantic import ValidationError

from app.models import AnomalyEvent
from app.settings import Settings
from app.state_machine import Orchestrator

try:
    from kafka.errors import NoBrokersAvailable
except ImportError:  # kafka-python >= 3.0 removes this symbol
    NoBrokersAvailable = KafkaConnectionError

log = logging.getLogger("orchestrator.kafka_consumer")

TOPIC = "anomalies.detected"
GROUP_ID = "orchestrator"


def _connect(settings: Settings, stop: threading.Event) -> KafkaConsumer | None:
    while not stop.is_set():
        try:
            return KafkaConsumer(
                TOPIC,
                bootstrap_servers=settings.kafka_bootstrap,
                group_id=GROUP_ID,
                value_deserializer=lambda v: json.loads(v.decode("utf-8")),
                key_deserializer=lambda k: k.decode("utf-8") if k else None,
                enable_auto_commit=False,
                # A restarted orchestrator should not re-open incidents for the last 24h of
                # anomalies it missed while down; new anomalies from here on are what matter.
                # handle_anomaly is idempotent on anomaly_id regardless, as a second guard.
                auto_offset_reset="latest",
                consumer_timeout_ms=1000,
            )
        except NoBrokersAvailable:
            log.warning("kafka not reachable yet, retrying in 3s")
            stop.wait(3)
    return None


def run(orchestrator: Orchestrator, settings: Settings, stop: threading.Event) -> None:
    consumer = _connect(settings, stop)
    if consumer is None:
        return
    log.info("listening on %s", TOPIC)
    while not stop.is_set():
        for msg in consumer:
            try:
                event = AnomalyEvent.model_validate(msg.value)
            except ValidationError as exc:
                log.error("dropping malformed anomalies.detected record: %s", exc)
                continue
            try:
                orchestrator.handle_anomaly(event)
            except Exception:
                log.exception("failed to handle anomaly_id=%s; will not be retried from this offset", event.anomaly_id)
            if stop.is_set():
                break
        consumer.commit()
    consumer.close()


def start(orchestrator: Orchestrator, settings: Settings) -> tuple[threading.Thread, threading.Event]:
    stop = threading.Event()
    thread = threading.Thread(target=run, args=(orchestrator, settings, stop), daemon=True, name="kafka-consumer")
    thread.start()
    return thread, stop
