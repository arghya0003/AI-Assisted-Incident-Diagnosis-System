"""TimescaleDB access for M3's own tables (timescaledb/init/005_diagnosis.sql).

The API opens a short-lived connection per request. /analyze is called at human pace and
will spend seconds in the LLM, so a pool would only add stale-connection handling. The Kafka
consumer keeps its own long-lived connection.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal, Protocol

import psycopg2
import psycopg2.errors
from psycopg2.extras import Json

from app.models import AnomalyEvent
from app.settings import Settings

M3_TABLES = ("anomalies", "incidents", "hypotheses")
MIGRATION = "timescaledb/init/005_diagnosis.sql"

SaveResult = Literal["inserted", "duplicate", "collision"]
DbStatus = Literal["ok", "unreachable", "schema_missing"]


class DatabaseUnavailable(RuntimeError):
    """TimescaleDB is unreachable, or M3's migration has not been applied to it."""


def connect(settings: Settings, connect_timeout: int = 5):
    return psycopg2.connect(
        host=settings.pg_host,
        port=settings.pg_port,
        dbname=settings.pg_db,
        user=settings.pg_user,
        password=settings.pg_password,
        connect_timeout=connect_timeout,
    )


_INSERT_ANOMALY = """
    INSERT INTO anomalies (anomaly_id, services, metrics, severity, t_detected, t_onset,
                           window_start, window_end, raw, source)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (anomaly_id) DO NOTHING
    RETURNING anomaly_id
"""


def save_anomaly(cur, event: AnomalyEvent, raw: dict, source: str = "kafka") -> SaveResult:
    """Store an anomaly unless its id is already present. Never overwrites.

    Kafka delivery is at-least-once, so a redelivered event is expected: "duplicate". The same
    id with different content is a "collision" — M2 derives ids from a millisecond clock
    (issue #4) — and the first event stored is kept rather than silently replaced.
    """
    window = event.evidence_window
    cur.execute(
        _INSERT_ANOMALY,
        (
            event.anomaly_id,
            event.services,
            event.metrics,
            event.severity,
            event.t_detected,
            event.t_onset,
            window.start,
            window.end,
            Json(raw),
            source,
        ),
    )
    if cur.fetchone() is not None:
        return "inserted"
    cur.execute("SELECT raw FROM anomalies WHERE anomaly_id = %s", (event.anomaly_id,))
    (existing,) = cur.fetchone()
    return "duplicate" if existing == raw else "collision"


def get_anomaly(cur, anomaly_id: str) -> AnomalyEvent | None:
    cur.execute("SELECT raw FROM anomalies WHERE anomaly_id = %s", (anomaly_id,))
    row = cur.fetchone()
    return None if row is None else AnomalyEvent.model_validate(row[0])


def missing_tables(cur) -> list[str]:
    cur.execute(
        "SELECT t FROM unnest(%s::text[]) AS t WHERE to_regclass(t) IS NULL",
        (list(M3_TABLES),),
    )
    return [table for (table,) in cur.fetchall()]


class AnomalyStore(Protocol):
    """What the API needs from storage; tests substitute an in-memory implementation."""

    def get(self, anomaly_id: str) -> AnomalyEvent | None: ...

    def status(self) -> DbStatus: ...


class PostgresAnomalyStore:
    def __init__(self, settings: Settings):
        self._settings = settings

    @contextmanager
    def _cursor(self) -> Iterator:
        try:
            conn = connect(self._settings)
        except psycopg2.OperationalError as exc:
            where = f"{self._settings.pg_host}:{self._settings.pg_port}"
            raise DatabaseUnavailable(f"cannot reach TimescaleDB at {where}") from exc
        try:
            with conn, conn.cursor() as cur:
                yield cur
        finally:
            conn.close()

    def get(self, anomaly_id: str) -> AnomalyEvent | None:
        try:
            with self._cursor() as cur:
                return get_anomaly(cur, anomaly_id)
        except psycopg2.errors.UndefinedTable as exc:
            raise DatabaseUnavailable(f"table 'anomalies' is missing; apply {MIGRATION}") from exc
        except psycopg2.OperationalError as exc:
            raise DatabaseUnavailable(f"TimescaleDB query failed: {exc}") from exc

    def status(self) -> DbStatus:
        try:
            with self._cursor() as cur:
                return "schema_missing" if missing_tables(cur) else "ok"
        except (DatabaseUnavailable, psycopg2.OperationalError):
            return "unreachable"
