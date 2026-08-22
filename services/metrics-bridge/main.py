"""
Scrapes the Phase 2 Prometheus server and republishes each derived metric
onto the metrics.raw Kafka topic, in the {service, metric, value,
timestamp, labels{}} shape from CONTRACTS.md.

Deliberately queries Prometheus (not each service's /metrics directly) so
the target list is discovered dynamically from whatever prometheus.yml is
currently scraping, rather than duplicating that list here.
"""

import os
import time
import json
import logging
from datetime import datetime, timezone

import requests
from kafka import KafkaProducer
from kafka.errors import NoBrokersAvailable

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("metrics-bridge")

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "kafka:9092")
SCRAPE_INTERVAL_SECONDS = float(os.environ.get("SCRAPE_INTERVAL_SECONDS", "5"))
TOPIC = "metrics.raw"

# One query per derived metric. {instance} is substituted with each live
# target's `instance` label (e.g. "catalogue:80"). Percentiles and rates
# use a 1m window so they stay meaningful at a 5s scrape interval.
METRIC_QUERIES = {
    "request_rate": 'sum(rate(request_duration_seconds_count{{instance="{instance}"}}[1m]))',
    "error_rate": (
        'sum(rate(request_duration_seconds_count{{instance="{instance}",status_code=~"5.."}}[1m])) '
        '/ clamp_min(sum(rate(request_duration_seconds_count{{instance="{instance}"}}[1m])), 1e-9)'
    ),
    "latency_p50_ms": (
        'histogram_quantile(0.50, sum(rate(request_duration_seconds_bucket{{instance="{instance}"}}[1m])) by (le)) * 1000'
    ),
    "latency_p95_ms": (
        'histogram_quantile(0.95, sum(rate(request_duration_seconds_bucket{{instance="{instance}"}}[1m])) by (le)) * 1000'
    ),
    "latency_p99_ms": (
        'histogram_quantile(0.99, sum(rate(request_duration_seconds_bucket{{instance="{instance}"}}[1m])) by (le)) * 1000'
    ),
    "cpu_rate": 'rate(process_cpu_seconds_total{{instance="{instance}"}}[1m])',
    "memory_bytes": 'process_resident_memory_bytes{{instance="{instance}"}}',
}


def connect_kafka() -> KafkaProducer:
    while True:
        try:
            return KafkaProducer(
                bootstrap_servers=KAFKA_BOOTSTRAP,
                value_serializer=lambda v: json.dumps(v).encode("utf-8"),
                key_serializer=lambda k: k.encode("utf-8") if k else None,
            )
        except NoBrokersAvailable:
            log.warning("kafka not reachable yet, retrying in 3s")
            time.sleep(3)


def list_live_instances() -> list[str]:
    resp = requests.get(f"{PROMETHEUS_URL}/api/v1/targets", timeout=5)
    resp.raise_for_status()
    targets = resp.json()["data"]["activeTargets"]
    return [t["labels"]["instance"] for t in targets if t["health"] == "up"]


def query_instant(promql: str) -> float | None:
    resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": promql}, timeout=5)
    resp.raise_for_status()
    result = resp.json()["data"]["result"]
    if not result:
        return None
    value = result[0]["value"][1]
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value != value:  # NaN check
        return None
    return value


def service_name_from_instance(instance: str) -> str:
    return instance.split(":", 1)[0]


def run():
    producer = connect_kafka()
    log.info("connected to kafka at %s, polling prometheus at %s every %ss",
              KAFKA_BOOTSTRAP, PROMETHEUS_URL, SCRAPE_INTERVAL_SECONDS)

    while True:
        cycle_start = time.time()
        try:
            instances = list_live_instances()
        except requests.RequestException as exc:
            log.warning("failed to list prometheus targets: %s", exc)
            time.sleep(SCRAPE_INTERVAL_SECONDS)
            continue

        published = 0
        for instance in instances:
            service = service_name_from_instance(instance)
            timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

            for metric_name, query_template in METRIC_QUERIES.items():
                promql = query_template.format(instance=instance)
                try:
                    value = query_instant(promql)
                except requests.RequestException as exc:
                    log.warning("query failed for %s/%s: %s", instance, metric_name, exc)
                    continue
                if value is None:
                    continue

                record = {
                    "service": service,
                    "metric": metric_name,
                    "value": value,
                    "timestamp": timestamp,
                    "labels": {"instance": instance},
                }
                producer.send(TOPIC, key=service, value=record)
                published += 1

        producer.flush()
        log.info("published %d samples for %d instances", published, len(instances))

        elapsed = time.time() - cycle_start
        time.sleep(max(0.0, SCRAPE_INTERVAL_SECONDS - elapsed))


if __name__ == "__main__":
    run()
