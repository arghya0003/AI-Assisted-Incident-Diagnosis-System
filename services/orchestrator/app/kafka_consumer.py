"""Consumes `anomalies.detected` (M2 -> M4, CONTRACTS.md) and opens an incident for each one.
Runs in a background thread started by main.py's lifespan, matching the connection-retry
pattern services/anomaly-detector/main.py and services/deploy-emitter/main.py already use.
"""

import json
import logging
import threading
from dataclasses import dataclass

from kafka import KafkaConsumer
from kafka.errors import KafkaError
from pydantic import ValidationError

from app.models import AnomalyEvent
from app.settings import Settings
from app.state_machine import Orchestrator


@dataclass
class ConsumerStatus:
    """Whether this thread is actually consuming, so a dead one is not silent.

    The failure this guards against is not a crash - the process stays up and
    the HTTP API keeps answering while nothing reads the topic at all. That is
    worse than an outage, because it looks like an absence of incidents rather
    than an absence of a consumer.
    """

    connected: bool = False
    exited: bool = False
    consumed: int = 0
    last_error: str | None = None

    def as_dict(self) -> dict:
        if self.exited:
            state = "exited"
        elif self.connected:
            state = "consuming"
        else:
            state = "connecting"
        out = {"state": state, "consumed": self.consumed}
        if self.last_error:
            out["last_error"] = self.last_error
        return out


STATUS = ConsumerStatus()

log = logging.getLogger("orchestrator.kafka_consumer")

TOPIC = "anomalies.detected"
GROUP_ID = "orchestrator"


def _connect(settings: Settings, stop: threading.Event) -> KafkaConsumer | None:
    """Retry until the broker answers, or until asked to stop.

    Catches KafkaError, not just the connection errors. On a cold start the
    broker is often up but not yet serving metadata, and kafka-python raises
    KafkaTimeoutError for that - which descends from RetriableError, NOT from
    KafkaConnectionError. Catching only the latter let that one escape, killing
    this thread while the rest of the service carried on looking healthy. It
    happened on two separate cold starts, each time leaving `anomalies.detected`
    unread for hours with a consumer group that had no members.
    """
    attempt = 0
    while not stop.is_set():
        try:
            consumer = KafkaConsumer(
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
        except KafkaError as exc:
            attempt += 1
            STATUS.connected = False
            STATUS.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("kafka not reachable yet (attempt %d: %s); retrying in 3s",
                        attempt, type(exc).__name__)
            stop.wait(3)
        else:
            STATUS.connected = True
            STATUS.last_error = None
            return consumer
    return None


def _consume(consumer: KafkaConsumer, orchestrator: Orchestrator, stop: threading.Event) -> None:
    while not stop.is_set():
        for msg in consumer:
            try:
                event = AnomalyEvent.model_validate(msg.value)
            except ValidationError as exc:
                log.error("dropping malformed anomalies.detected record: %s", exc)
                continue
            try:
                orchestrator.handle_anomaly(event)
                STATUS.consumed += 1
            except Exception:
                log.exception("failed to handle anomaly_id=%s; will not be retried from this offset", event.anomaly_id)
            if stop.is_set():
                break
        consumer.commit()


def run(orchestrator: Orchestrator, settings: Settings, stop: threading.Event) -> None:
    """Consume until asked to stop, reconnecting if the broker goes away.

    A broker that disappears mid-run used to end this thread, the same silent
    way a failed first connection did. Reconnecting instead means a Kafka
    restart costs a gap, not the decision path.
    """
    try:
        while not stop.is_set():
            consumer = _connect(settings, stop)
            if consumer is None:
                return  # asked to stop while connecting
            log.info("listening on %s", TOPIC)
            try:
                _consume(consumer, orchestrator, stop)
            except KafkaError as exc:
                STATUS.connected = False
                STATUS.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("kafka consumer dropped (%s: %s); reconnecting",
                            type(exc).__name__, exc)
                stop.wait(3)
            else:
                break  # clean stop
            finally:
                try:
                    consumer.close()
                except Exception:  # noqa: BLE001 - closing a broken consumer must not mask why
                    pass
    finally:
        STATUS.connected = False
        STATUS.exited = True
        # The loudest line in this module on purpose. Everything else about the
        # service keeps working when this thread dies, so without an explicit
        # record the only symptom is incidents quietly never being opened.
        log.warning("kafka consumer thread has exited; %s is no longer being consumed", TOPIC)


def start(orchestrator: Orchestrator, settings: Settings) -> tuple[threading.Thread, threading.Event]:
    stop = threading.Event()
    thread = threading.Thread(target=run, args=(orchestrator, settings, stop), daemon=True, name="kafka-consumer")
    thread.start()
    return thread, stop
