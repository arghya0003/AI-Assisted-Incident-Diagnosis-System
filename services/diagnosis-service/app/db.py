"""TimescaleDB access for M3's own tables (timescaledb/init/005_diagnosis.sql), plus read-only
queries against M1's `deploys` table.

The API opens a short-lived connection per request. /analyze is called at human pace and
will spend seconds in the LLM, so a pool would only add stale-connection handling. The Kafka
consumer keeps its own long-lived connection.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Literal, Protocol, TypeVar

import psycopg2
import psycopg2.errors
from psycopg2.extras import Json

from app.corpus import IncidentRecord, section
from app.models import AnomalyEvent, Deploy, SimilarIncident
from app.scoring import ScoringInputs
from app.settings import Settings

M3_TABLES = ("anomalies", "incidents", "hypotheses")
MIGRATION = "timescaledb/init/005_diagnosis.sql"

SaveResult = Literal["inserted", "duplicate", "collision"]
DbStatus = Literal["ok", "unreachable", "schema_missing"]
T = TypeVar("T")


class DatabaseUnavailable(RuntimeError):
    """TimescaleDB is unreachable, or a table this service reads has not been created."""


def connect(settings: Settings, connect_timeout: int = 5):
    return psycopg2.connect(
        host=settings.pg_host,
        port=settings.pg_port,
        dbname=settings.pg_db,
        user=settings.pg_user,
        password=settings.pg_password,
        connect_timeout=connect_timeout,
    )


# ------------------------------------------------------------------ anomalies

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


# Anomalies from the same source (never mixing fixtures with real events) whose onset is within
# the window either side of this anomaly's onset.
_RELATED_ANOMALIES = """
    SELECT other.raw
    FROM anomalies AS this
    JOIN anomalies AS other
      ON other.source = this.source
     AND other.anomaly_id <> this.anomaly_id
     AND other.t_onset BETWEEN this.t_onset - make_interval(secs => %(window)s)
                           AND this.t_onset + make_interval(secs => %(window)s)
    WHERE this.anomaly_id = %(anomaly_id)s
    ORDER BY other.t_onset, other.anomaly_id
"""

_DEPLOYS_BEFORE_ONSET = """
    SELECT deploy_id, service, version, commit_sha, config_diff, time
    FROM deploys
    WHERE time BETWEEN %(onset)s - make_interval(secs => %(lookback)s * 60) AND %(onset)s
    ORDER BY time DESC, deploy_id
"""


def get_scoring_inputs(
    cur, anomaly_id: str, window_seconds: float, lookback_minutes: float
) -> ScoringInputs | None:
    """Everything candidate scoring needs for one anomaly from the database, in one transaction.
    Similar incidents are added separately, because retrieval also needs the embedding model."""
    anomaly = get_anomaly(cur, anomaly_id)
    if anomaly is None:
        return None
    cur.execute(_RELATED_ANOMALIES, {"anomaly_id": anomaly_id, "window": window_seconds})
    related = [AnomalyEvent.model_validate(raw) for (raw,) in cur.fetchall()]
    cur.execute(_DEPLOYS_BEFORE_ONSET, {"onset": anomaly.t_onset, "lookback": lookback_minutes})
    columns = [column.name for column in cur.description]
    deploys = [Deploy.model_validate(dict(zip(columns, row))) for row in cur.fetchall()]
    return ScoringInputs(anomaly=anomaly, related=related, deploys=deploys)


# ------------------------------------------------------------------ incidents


def vector_literal(vector: list[float]) -> str:
    """pgvector's text form, so no pgvector Python adapter is needed."""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


_UPSERT_INCIDENT = """
    INSERT INTO incidents (incident_id, title, body, services, fault_type, source, embedding)
    VALUES (%(incident_id)s, %(title)s, %(body)s, %(services)s, %(fault_type)s, %(source)s,
            %(embedding)s::vector)
    ON CONFLICT (incident_id) DO UPDATE SET
        title = EXCLUDED.title,
        body = EXCLUDED.body,
        services = EXCLUDED.services,
        fault_type = EXCLUDED.fault_type,
        source = EXCLUDED.source,
        embedding = EXCLUDED.embedding
"""


def upsert_incident(cur, record: IncidentRecord, vector: list[float]) -> None:
    cur.execute(
        _UPSERT_INCIDENT,
        {
            "incident_id": record.incident_id,
            "title": record.title,
            "body": record.body,
            "services": record.services,
            "fault_type": record.fault_type,
            "source": record.source,
            "embedding": vector_literal(vector),
        },
    )


def delete_incidents_except(cur, keep_ids: list[str]) -> int:
    """Remove incidents whose corpus file no longer exists. Returns the number deleted."""
    cur.execute("DELETE FROM incidents WHERE NOT (incident_id = ANY(%s::text[]))", (list(keep_ids),))
    return cur.rowcount


def count_incidents(cur) -> int:
    cur.execute("SELECT count(*) FROM incidents WHERE embedding IS NOT NULL")
    return cur.fetchone()[0]


# Hybrid keeps an incident if it names a candidate service OR its fault type fits the metrics.
# A NULL services array or fault type simply fails its half of the filter.
_SEARCH_INCIDENTS = """
    SELECT incident_id, title, services, fault_type, source, body,
           1 - (embedding <=> %(query)s::vector) AS similarity
    FROM incidents
    WHERE embedding IS NOT NULL
      AND (NOT %(hybrid)s
           OR services && %(services)s::text[]
           OR fault_type = ANY(%(fault_types)s::text[]))
    ORDER BY embedding <=> %(query)s::vector, incident_id
    LIMIT %(top_k)s
"""


def search_incidents(
    cur, vector: list[float], services: list[str], fault_types: list[str], top_k: int, hybrid: bool
) -> list[SimilarIncident]:
    cur.execute(
        _SEARCH_INCIDENTS,
        {
            "query": vector_literal(vector),
            "services": list(services),
            "fault_types": list(fault_types),
            "top_k": top_k,
            "hybrid": hybrid,
        },
    )
    return [
        SimilarIncident(
            incident_id=incident_id,
            title=title,
            services=services or [],
            fault_type=fault_type,
            source=source,
            # Float rounding can put an identical vector a hair outside [-1, 1].
            similarity=max(-1.0, min(1.0, similarity)),
            root_cause=section(body, "Root cause"),
            resolution=section(body, "Resolution"),
        )
        for incident_id, title, services, fault_type, source, body, similarity in cur.fetchall()
    ]


def missing_tables(cur) -> list[str]:
    cur.execute(
        "SELECT t FROM unnest(%s::text[]) AS t WHERE to_regclass(t) IS NULL",
        (list(M3_TABLES),),
    )
    return [table for (table,) in cur.fetchall()]


class AnomalyStore(Protocol):
    """What the API needs from storage; tests substitute an in-memory implementation."""

    def get(self, anomaly_id: str) -> AnomalyEvent | None: ...

    def scoring_inputs(
        self, anomaly_id: str, window_seconds: float, lookback_minutes: float
    ) -> ScoringInputs | None: ...

    def incident_count(self) -> int: ...

    def search_incidents(
        self, vector: list[float], services: list[str], fault_types: list[str], top_k: int, hybrid: bool
    ) -> list[SimilarIncident]: ...

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

    def _query(self, query: Callable[..., T], *args) -> T:
        try:
            with self._cursor() as cur:
                return query(cur, *args)
        except psycopg2.errors.UndefinedTable as exc:
            raise DatabaseUnavailable(
                f"a required table is missing ({exc.diag.message_primary}); apply {MIGRATION}"
            ) from exc
        except psycopg2.OperationalError as exc:
            raise DatabaseUnavailable(f"TimescaleDB query failed: {exc}") from exc

    def get(self, anomaly_id: str) -> AnomalyEvent | None:
        return self._query(get_anomaly, anomaly_id)

    def scoring_inputs(
        self, anomaly_id: str, window_seconds: float, lookback_minutes: float
    ) -> ScoringInputs | None:
        return self._query(get_scoring_inputs, anomaly_id, window_seconds, lookback_minutes)

    def incident_count(self) -> int:
        return self._query(count_incidents)

    def search_incidents(
        self, vector: list[float], services: list[str], fault_types: list[str], top_k: int, hybrid: bool
    ) -> list[SimilarIncident]:
        return self._query(search_incidents, vector, services, fault_types, top_k, hybrid)

    def status(self) -> DbStatus:
        try:
            with self._cursor() as cur:
                return "schema_missing" if missing_tables(cur) else "ok"
        except (DatabaseUnavailable, psycopg2.OperationalError):
            return "unreachable"
