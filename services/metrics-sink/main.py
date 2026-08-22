"""
Consumes metrics.raw off Kafka and writes each sample into TimescaleDB's
`metrics` hypertable. Batches a short poll window into one INSERT, then
commits Kafka offsets - so a crash before commit just re-reads a few
already-written rows (at-least-once, fine for a metrics stream).
"""

import os
import json
import logging
import time

import psycopg2
import psycopg2.extras
from kafka import KafkaConsumer
from kafka.errors import NoBrokersAvailable

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("metrics-sink")

KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "kafka:9092")
TOPIC = "metrics.raw"
GROUP_ID = "metrics-sink"

PG_HOST = os.environ.get("PG_HOST", "timescaledb")
PG_PORT = os.environ.get("PG_PORT", "5432")
PG_DB = os.environ.get("PG_DB", "metrics")
PG_USER = os.environ.get("PG_USER", "postgres")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "Abcd1234#")

BATCH_MAX_RECORDS = 200

INSERT_SQL = """
    INSERT INTO metrics (time, service, metric, value, labels)
    VALUES %s
"""


def connect_kafka() -> KafkaConsumer:
    while True:
        try:
            return KafkaConsumer(
                TOPIC,
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


def connect_postgres():
    while True:
        try:
            conn = psycopg2.connect(
                host=PG_HOST, port=PG_PORT, dbname=PG_DB,
                user=PG_USER, password=PG_PASSWORD,
            )
            conn.autocommit = False
            return conn
        except psycopg2.OperationalError as exc:
            log.warning("timescaledb not reachable yet (%s), retrying in 3s", exc)
            time.sleep(3)


def record_to_row(record: dict):
    return (
        record["timestamp"],
        record["service"],
        record["metric"],
        record["value"],
        json.dumps(record.get("labels") or {}),
    )


def run():
    consumer = connect_kafka()
    conn = connect_postgres()
    cur = conn.cursor()
    log.info("consuming %s from %s, writing to postgres://%s:%s/%s",
              TOPIC, KAFKA_BOOTSTRAP, PG_HOST, PG_PORT, PG_DB)

    batch = []

    def flush():
        nonlocal batch
        if not batch:
            return
        psycopg2.extras.execute_values(cur, INSERT_SQL, batch)
        conn.commit()
        consumer.commit()
        log.info("wrote %d rows", len(batch))
        batch = []

    while True:
        # consumer_timeout_ms bounds each pass of this loop to ~1s even
        # with no new messages, so a pending batch never sits unflushed
        # for long - keeps end-to-end lag low during quiet periods.
        for message in consumer:
            try:
                batch.append(record_to_row(message.value))
            except (KeyError, TypeError) as exc:
                log.warning("skipping malformed record %r: %s", message.value, exc)
                continue

            if len(batch) >= BATCH_MAX_RECORDS:
                flush()

        flush()


if __name__ == "__main__":
    run()
