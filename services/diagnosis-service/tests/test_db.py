"""Phase 2 integration: app.db and the consumer's message handling against real TimescaleDB.

Skipped unless TimescaleDB is reachable (localhost:5432 by default; set TEST_PG_HOST to
override) with 005_diagnosis.sql applied. Each test runs in one transaction that is rolled
back, so nothing is left in the shared dev database.
"""

import dataclasses
import json
import os

import psycopg2
import pytest

from app.consumer import handle_message
from app.db import (
    DatabaseUnavailable,
    PostgresAnomalyStore,
    connect,
    get_anomaly,
    missing_tables,
    save_anomaly,
)
from app.models import AnomalyEvent
from app.settings import settings

TEST_SETTINGS = dataclasses.replace(settings, pg_host=os.environ.get("TEST_PG_HOST", "localhost"))

EVENT = {
    "anomaly_id": "anom-test-phase2-0001",
    "services": ["catalogue"],
    "metrics": ["latency_p95_ms"],
    "severity": "high",
    "t_detected": "2026-09-13T08:08:13.234Z",
    "t_onset": "2026-09-13T08:08:13.221Z",
    "evidence_window": {"start": "2026-09-13T08:08:13.221Z", "end": "2026-09-13T08:08:13.221Z"},
}


@pytest.fixture
def cur():
    try:
        conn = connect(TEST_SETTINGS, connect_timeout=2)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"TimescaleDB not reachable: {exc}")
    try:
        with conn.cursor() as cursor:
            missing = missing_tables(cursor)
            if missing:
                pytest.skip(f"005_diagnosis.sql not applied; missing tables {missing}")
            yield cursor
    finally:
        conn.rollback()
        conn.close()


def _save(cur, raw):
    return save_anomaly(cur, AnomalyEvent.model_validate(raw), raw)


def test_save_then_get_round_trips(cur):
    assert _save(cur, EVENT) == "inserted"
    assert get_anomaly(cur, EVENT["anomaly_id"]) == AnomalyEvent.model_validate(EVENT)
    cur.execute(
        "SELECT source, services, window_start = window_end FROM anomalies WHERE anomaly_id = %s",
        (EVENT["anomaly_id"],),
    )
    assert cur.fetchone() == ("kafka", ["catalogue"], True)


def test_redelivery_is_a_duplicate(cur):
    _save(cur, EVENT)
    assert _save(cur, EVENT) == "duplicate"


def test_same_id_different_content_is_a_collision_and_keeps_the_first(cur):
    _save(cur, EVENT)
    assert _save(cur, {**EVENT, "services": ["user"]}) == "collision"
    assert get_anomaly(cur, EVENT["anomaly_id"]).services == ["catalogue"]


def test_raw_keeps_fields_m2_adds_later(cur):
    _save(cur, {**EVENT, "z_score": 4.2})
    cur.execute("SELECT raw->>'z_score' FROM anomalies WHERE anomaly_id = %s", (EVENT["anomaly_id"],))
    assert cur.fetchone() == ("4.2",)


def test_unknown_anomaly_is_none(cur):
    assert get_anomaly(cur, "anom-test-phase2-does-not-exist") is None


@pytest.mark.parametrize(
    "value",
    [None, b"not json", b"\xff\xfe", b"[1, 2]", json.dumps({**EVENT, "services": []}).encode()],
    ids=["tombstone", "not-json", "not-utf8", "not-an-object", "fails-validation"],
)
def test_consumer_skips_malformed_messages(cur, value):
    assert handle_message(cur, value) == "invalid"


def test_consumer_stores_a_valid_message(cur):
    assert handle_message(cur, json.dumps(EVENT).encode()) == "inserted"
    assert handle_message(cur, json.dumps(EVENT).encode()) == "duplicate"


def test_store_reports_ok_against_the_real_database(cur):
    store = PostgresAnomalyStore(TEST_SETTINGS)
    assert store.status() == "ok"
    assert store.get("anom-test-phase2-does-not-exist") is None


def test_store_reports_an_unreachable_database():
    store = PostgresAnomalyStore(dataclasses.replace(settings, pg_host="127.0.0.1", pg_port=1))
    assert store.status() == "unreachable"
    with pytest.raises(DatabaseUnavailable):
        store.get("anom-0001")
