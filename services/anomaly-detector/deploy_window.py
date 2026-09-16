"""
Deploy-window awareness.

A deploy makes a service briefly misbehave for boring reasons: the container
restarts, caches are cold, connection pools refill. Those blips are not
incidents, and alerting on every one of them is how a detector earns a
reputation for crying wolf.

The obvious implementation — mute a service while it is mid-deploy — is
wrong here, and badly so. The single most important fault class this project
diagnoses is `bad_deploy_latency`: a deploy that genuinely breaks a service.
Hard suppression would mute precisely the incidents the system exists to
catch, and would do it silently.

So a deploy window raises the evidence bar instead of closing the gate. While
a service is inside one, a breach must persist across more consecutive
samples before it fires. A cold-start blip lasting a sample or two is
filtered; a real regression that persists still fires, and arrives tagged
with the `deploy_id` that M3 needs in order to correlate it.

Seasonality suppression is deliberately not implemented. This testbed is
driven by synthetic traffic with no diurnal or weekly cycle, so a
time-of-day baseline would model noise and be impossible to validate. It is
recorded as a known gap rather than built as untested ceremony — see
docs/phase9-detection.md.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

log = logging.getLogger("deploy-window")

TOPIC = "deploys.events"


@dataclass(frozen=True)
class DeployRecord:
    deploy_id: str
    service: str
    at: datetime


class DeployWindowTracker:
    """Remembers the most recent deploy per service and scores the window.

    Updated from a background Kafka consumer thread and read from the
    detection loop, so all shared state is guarded by a lock.
    """

    def __init__(self, window_seconds: float = 120.0, breaches_in_window: int = 4):
        self.window = timedelta(seconds=window_seconds)
        self.breaches_in_window = breaches_in_window
        self._latest: dict[str, DeployRecord] = {}
        self._lock = threading.Lock()

    def record(self, deploy_id: str, service: str, at: datetime) -> None:
        with self._lock:
            current = self._latest.get(service)
            if current is None or at >= current.at:
                self._latest[service] = DeployRecord(deploy_id, service, at)

    def active_deploy(self, service: str, now: datetime) -> DeployRecord | None:
        with self._lock:
            record = self._latest.get(service)
        if record is None:
            return None
        return record if now - record.at <= self.window else None

    def required_breaches(self, service: str, now: datetime, default: int) -> tuple[int, str | None]:
        """Return (breaches needed to fire, deploy_id if inside a window)."""
        record = self.active_deploy(service, now)
        if record is None:
            return default, None
        return max(default, self.breaches_in_window), record.deploy_id


def consume_deploys(tracker: DeployWindowTracker, bootstrap: str, stop: threading.Event) -> None:
    """Background loop feeding the tracker from `deploys.events`."""
    from kafka import KafkaConsumer
    from kafka.errors import KafkaError

    consumer = None
    while not stop.is_set():
        try:
            if consumer is None:
                consumer = KafkaConsumer(
                    TOPIC,
                    bootstrap_servers=bootstrap,
                    group_id=None,  # every detector instance wants every deploy
                    value_deserializer=lambda v: json.loads(v.decode("utf-8")),
                    enable_auto_commit=False,
                    auto_offset_reset="latest",
                    consumer_timeout_ms=1000,
                )
                log.info("tracking deploy windows from %s", TOPIC)

            for msg in consumer:
                record = msg.value
                service = record.get("service")
                deploy_id = record.get("deploy_id")
                timestamp = record.get("timestamp")
                if not service or not deploy_id or not timestamp:
                    continue
                at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                tracker.record(deploy_id, service, at)
                log.info("deploy window open for %s (%s)", service, deploy_id)
        except KafkaError as exc:
            log.warning("deploy consumer error (%s), reconnecting in 3s", exc)
            consumer = None
            time.sleep(3)
