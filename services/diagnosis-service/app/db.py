"""TimescaleDB access for M3's own tables (timescaledb/init/005_diagnosis.sql and
006_diagnosis_analyses.sql), plus read-only queries against M1's `deploys` table and M2's
`anomalies` table.

M2's detector writes every published anomaly to `anomalies` (005_anomalies.sql, shape agreed in
PR #10), so this service only reads it; `save_anomaly` is for the hand-written fixtures.

The API opens a short-lived connection per request. /analyze is called at human pace and will
spend seconds in the LLM, so a pool would only add stale-connection handling.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Literal, Protocol, TypeVar

import psycopg2
import psycopg2.errors
from psycopg2.extras import Json

from app.corpus import IncidentRecord, section
from app.models import REUSABLE_ANSWERS, AnomalyEvent, Deploy, Evidence, SimilarIncident, StoredAnalysis
from app.scoring import ScoringInputs
from app.settings import Settings

M3_TABLES = ("anomalies", "incidents", "hypotheses", "analyses")
MIGRATION = "timescaledb/init/005_anomalies.sql, 005_diagnosis.sql and 006_diagnosis_analyses.sql"

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
    INSERT INTO anomalies (anomaly_id, detector, services, metrics, severity, t_detected, t_onset,
                           evidence_window_start, evidence_window_end, raw, source)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (anomaly_id) DO NOTHING
    RETURNING anomaly_id
"""


def save_anomaly(cur, event: AnomalyEvent, raw: dict, detector: str, source: str) -> SaveResult:
    """Store an anomaly unless its id is already present. Never overwrites.

    Real events are written by M2's detector; this is how fixtures get into the same table, with
    `source='fixture'` so evaluation can exclude them. The same id with different content is a
    "collision", and the row already there is kept rather than silently replaced.
    """
    window = event.evidence_window
    cur.execute(
        _INSERT_ANOMALY,
        (
            event.anomaly_id,
            detector,
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


# ------------------------------------------------------------------ analyses

_INSERT_ANALYSIS = """
    INSERT INTO analyses (analysis_id, anomaly_id, pipeline_mode, answered_by, model_version, config_fingerprint,
                          llm_attempts, guardrail_rejected, latency_ms, fallback_reason, created_at)
    VALUES (%(analysis_id)s, %(anomaly_id)s, %(pipeline_mode)s, %(answered_by)s, %(model_version)s,
            %(config_fingerprint)s, %(llm_attempts)s, %(guardrail_rejected)s, %(latency_ms)s, %(fallback_reason)s,
            clock_timestamp())
    RETURNING created_at
"""

_INSERT_HYPOTHESIS = """
    INSERT INTO hypotheses (hypothesis_id, analysis_id, anomaly_id, rank, service, cause, confidence, evidence_ids,
                            proposed_action, model_version, pipeline_mode, latency_ms)
    VALUES (%(hypothesis_id)s, %(analysis_id)s, %(anomaly_id)s, %(rank)s, %(service)s, %(cause)s, %(confidence)s,
            %(evidence_ids)s, %(proposed_action)s, %(model_version)s, %(pipeline_mode)s, %(latency_ms)s)
"""

# Evidence ids are deterministic per anomaly, so re-analysing an anomaly updates its rows in place.
_UPSERT_EVIDENCE = """
    INSERT INTO evidence (evidence_id, incident_id, category, source_id, service, observed_at, relevance, summary, payload)
    VALUES (%(evidence_id)s, %(incident_id)s, %(category)s, %(source_id)s, %(service)s, %(observed_at)s,
            %(relevance)s, %(summary)s, %(payload)s)
    ON CONFLICT (evidence_id) DO UPDATE SET
        observed_at = EXCLUDED.observed_at,
        relevance = EXCLUDED.relevance,
        summary = EXCLUDED.summary,
        payload = EXCLUDED.payload
"""


def save_analysis(cur, analysis: StoredAnalysis, evidence: list[Evidence]) -> datetime:
    """Store one run, its hypotheses, and the evidence behind them. Returns the stored created_at, taken
    from clock_timestamp() so runs saved in one transaction still order correctly."""
    cur.execute(_INSERT_ANALYSIS, analysis.model_dump(exclude={"hypotheses", "created_at"}))
    (created_at,) = cur.fetchone()
    for hypothesis in analysis.hypotheses:
        cur.execute(
            _INSERT_HYPOTHESIS,
            {
                **hypothesis.model_dump(),
                "hypothesis_id": f"{analysis.analysis_id}-{hypothesis.rank}",
                "analysis_id": analysis.analysis_id,
                "anomaly_id": analysis.anomaly_id,
                "model_version": analysis.model_version,
                "pipeline_mode": analysis.pipeline_mode,
                "latency_ms": analysis.latency_ms,
            },
        )
    for item in evidence:
        cur.execute(_UPSERT_EVIDENCE, {**item.model_dump(), "payload": Json(item.payload)})
    return created_at


_ANALYSES = """
    SELECT a.analysis_id, a.anomaly_id, a.pipeline_mode, a.answered_by, a.model_version, a.config_fingerprint,
           a.llm_attempts, a.guardrail_rejected, a.latency_ms, a.fallback_reason, a.created_at,
           COALESCE(
               json_agg(json_build_object(
                   'rank', h.rank, 'service', h.service, 'cause', h.cause, 'confidence', h.confidence,
                   'evidence_ids', h.evidence_ids, 'proposed_action', h.proposed_action
               ) ORDER BY h.rank) FILTER (WHERE h.hypothesis_id IS NOT NULL),
               '[]'::json
           ) AS hypotheses
    FROM analyses AS a
    LEFT JOIN hypotheses AS h ON h.analysis_id = a.analysis_id
    WHERE a.anomaly_id = %(anomaly_id)s
      AND (%(pipeline_mode)s::text IS NULL OR a.pipeline_mode = %(pipeline_mode)s)
      AND (%(model_version)s::text IS NULL OR a.model_version = %(model_version)s)
      AND (%(config_fingerprint)s::text IS NULL OR a.config_fingerprint = %(config_fingerprint)s)
      AND (NOT %(reusable_only)s OR a.answered_by = ANY(%(reusable)s))
    GROUP BY a.analysis_id
    ORDER BY a.created_at DESC, a.analysis_id
    LIMIT %(limit)s
"""


def list_analyses(
    cur,
    anomaly_id: str,
    pipeline_mode: str | None = None,
    model_version: str | None = None,
    config_fingerprint: str | None = None,
    reusable_only: bool = False,
    limit: int = 20,
) -> list[StoredAnalysis]:
    """Stored runs for an anomaly, newest first, each with its hypotheses in rank order."""
    cur.execute(
        _ANALYSES,
        {
            "anomaly_id": anomaly_id,
            "pipeline_mode": pipeline_mode,
            "model_version": model_version,
            "config_fingerprint": config_fingerprint,
            "reusable_only": reusable_only,
            "reusable": list(REUSABLE_ANSWERS),
            "limit": limit,
        },
    )
    columns = [column.name for column in cur.description]
    return [StoredAnalysis.model_validate(dict(zip(columns, row))) for row in cur.fetchall()]


def latest_reusable_analysis(
    cur, anomaly_id: str, pipeline_mode: str, model_version: str, config_fingerprint: str
) -> StoredAnalysis | None:
    found = list_analyses(cur, anomaly_id, pipeline_mode, model_version, config_fingerprint, reusable_only=True, limit=1)
    return found[0] if found else None


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

    def save_analysis(self, analysis: StoredAnalysis, evidence: list[Evidence]) -> datetime: ...

    def latest_reusable_analysis(
        self, anomaly_id: str, pipeline_mode: str, model_version: str, config_fingerprint: str
    ) -> StoredAnalysis | None: ...

    def analyses(self, anomaly_id: str, pipeline_mode: str | None = None, limit: int = 20) -> list[StoredAnalysis]: ...

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
        except (psycopg2.errors.UndefinedTable, psycopg2.errors.UndefinedColumn) as exc:
            raise DatabaseUnavailable(
                f"the database schema is out of date ({exc.diag.message_primary}); apply {MIGRATION}"
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

    def save_analysis(self, analysis: StoredAnalysis, evidence: list[Evidence]) -> datetime:
        return self._query(save_analysis, analysis, evidence)

    def latest_reusable_analysis(
        self, anomaly_id: str, pipeline_mode: str, model_version: str, config_fingerprint: str
    ) -> StoredAnalysis | None:
        return self._query(latest_reusable_analysis, anomaly_id, pipeline_mode, model_version, config_fingerprint)

    def analyses(self, anomaly_id: str, pipeline_mode: str | None = None, limit: int = 20) -> list[StoredAnalysis]:
        return self._query(list_analyses, anomaly_id, pipeline_mode, None, None, False, limit)

    def status(self) -> DbStatus:
        try:
            with self._cursor() as cur:
                return "schema_missing" if missing_tables(cur) else "ok"
        except (DatabaseUnavailable, psycopg2.OperationalError):
            return "unreachable"
