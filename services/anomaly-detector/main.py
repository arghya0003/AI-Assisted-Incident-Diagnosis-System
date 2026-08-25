import os
import json
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from kafka import KafkaConsumer, KafkaProducer
from kafka.errors import KafkaConnectionError

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


@dataclass
class EWMAAnomalyDetector:
    alpha: float = 0.2
    z_threshold: float = 3.0
    warmup: int = 10
    min_samples: int = 15
    service: str = "catalogue"
    metric: str = "latency_p99_ms"
    _mean: float | None = None
    _variance: float | None = None
    _count: int = 0
    _ewma: float | None = None
    _ewmvar: float | None = None

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

        # update the baseline after deciding whether the sample is anomalous
        prev = self._ewma
        self._ewma = self.alpha * value + (1 - self.alpha) * prev
        self._ewmvar = self.alpha * (value - prev) ** 2 + (1 - self.alpha) * (self._ewmvar or 0.0)
        self._count += 1

        if z >= self.z_threshold:
            severity = "high" if z >= 4.5 else "medium"
            return {
                "anomaly_id": f"anom-{int(time.time() * 1000)}",
                "service": self.service,
                "metric": self.metric,
                "services": [self.service],
                "metrics": [self.metric],
                "severity": severity,
                "t_detected": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "t_onset": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "evidence_window": {
                    "start": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    "end": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                },
                "value": value,
                "baseline": prev_mean,
                "z_score": z,
            }
        return None


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
                auto_offset_reset="earliest",
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


def run():
    consumer = connect_kafka_consumer()
    producer = connect_kafka_producer()
    log.info("listening on %s and publishing to %s", INPUT_TOPIC, OUTPUT_TOPIC)

    detectors: dict[tuple[str, str], EWMAAnomalyDetector] = {}

    while True:
        for msg in consumer:
            record = msg.value
            service = record.get("service")
            metric = record.get("metric")
            value = record.get("value")
            if not service or not metric or value is None:
                continue

            key = (service, metric)
            if key not in detectors:
                detectors[key] = EWMAAnomalyDetector(service=service, metric=metric)

            detector = detectors[key]
            anomaly = detector.update(float(value))

            if anomaly is not None:
                anomaly["services"] = [service]
                anomaly["metrics"] = [metric]
                anomaly["t_onset"] = record.get("timestamp", anomaly["t_onset"])
                anomaly["evidence_window"] = {
                    "start": record.get("timestamp", anomaly["evidence_window"]["start"]),
                    "end": record.get("timestamp", anomaly["evidence_window"]["end"]),
                }
                producer.send(OUTPUT_TOPIC, key=service, value={
                    "anomaly_id": anomaly["anomaly_id"],
                    "services": anomaly["services"],
                    "metrics": anomaly["metrics"],
                    "severity": anomaly["severity"],
                    "t_detected": anomaly["t_detected"],
                    "t_onset": anomaly["t_onset"],
                    "evidence_window": anomaly["evidence_window"],
                })
                producer.flush()
                log.info("emitted anomaly for %s/%s: %s", service, metric, anomaly)

        # yield to the consumer loop without busy-spin; keep the process responsive
        time.sleep(0.5)


if __name__ == "__main__":
    run()
