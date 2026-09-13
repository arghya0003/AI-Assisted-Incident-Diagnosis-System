"""Past-incident retrieval (RAG).

Hybrid by default: a structured pre-filter keeps incidents that name one of the candidate
services or whose fault type fits the anomaly's metrics, then pgvector cosine similarity ranks
what remains. RETRIEVAL_MODE=vector skips the pre-filter, for comparison.

Retrieval never fails a request. If the embedding model is unreachable, the result says so and
scoring proceeds with incident similarity 0; the deterministic baseline must not depend on Ollama.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

from app.corpus import IncidentRecord
from app.graph import DEFAULT_MAX_HOPS, DependencyGraph
from app.models import AnomalyEvent, SimilarIncident
from app.ollama import OllamaUnavailable

log = logging.getLogger("diagnosis-service.retrieval")

EMBEDDING_DIMENSIONS = 768  # nomic-embed-text; matches incidents.embedding vector(768)
# nomic-embed-text is trained with task prefixes; embedding without them degrades similarity.
QUERY_PREFIX = "search_query: "
DOCUMENT_PREFIX = "search_document: "

Embed = Callable[[list[str]], list[list[float]]]
RetrievalMode = Literal["hybrid", "vector"]
RetrievalStatus = Literal["not_run", "ok", "empty_corpus", "embedding_unavailable"]

METRIC_PHRASES = {
    "latency_p50_ms": "median latency",
    "latency_p95_ms": "p95 latency",
    "latency_p99_ms": "p99 latency",
    "error_rate": "error rate",
    "cpu_rate": "CPU usage",
    "memory_bytes": "memory usage",
    "request_rate": "request rate",
}

_LATENCY_FAULTS = frozenset({"bad_deploy_latency", "db_pool_saturation", "db_contention", "capacity"})
# Which incident fault types an anomalous metric is consistent with. A heuristic for the
# pre-filter only, deliberately generous: excluding the right incident costs more than
# including an extra one, because similarity still ranks the survivors.
SUSPECTED_FAULT_TYPES: dict[str, frozenset[str]] = {
    "latency_p50_ms": _LATENCY_FAULTS,
    "latency_p95_ms": _LATENCY_FAULTS,
    "latency_p99_ms": _LATENCY_FAULTS,
    "error_rate": frozenset(
        {"service_crash", "bad_deploy_errors", "db_pool_saturation", "dependency_failure", "config_error"}
    ),
    "cpu_rate": frozenset({"bad_deploy_latency", "capacity"}),
    "memory_bytes": frozenset({"capacity", "benign"}),
    "request_rate": frozenset({"capacity"}),
}


def query_text(anomaly: AnomalyEvent) -> str:
    # M2's event carries no value or baseline, so the deviation size can't be included.
    metrics = ", ".join(METRIC_PHRASES.get(metric, metric) for metric in anomaly.metrics)
    return f"{QUERY_PREFIX}{anomaly.severity} severity anomaly: abnormal {metrics} on {', '.join(anomaly.services)}"


def document_text(record: IncidentRecord) -> str:
    # Only the symptoms are embedded. An anomaly query can only describe symptoms, and embedding
    # the title, root cause and resolution as well let a few short, generic write-ups match
    # almost every query (PLAN.md, Phase 5 outcome). The full body is still stored for the prompt.
    return f"{DOCUMENT_PREFIX}{record.symptoms}"


def suspected_fault_types(metrics: list[str]) -> set[str]:
    return set().union(*(SUSPECTED_FAULT_TYPES.get(metric, frozenset()) for metric in metrics))


def candidate_services(anomaly: AnomalyEvent, graph: DependencyGraph, max_hops: int = DEFAULT_MAX_HOPS) -> set[str]:
    """The same candidate set scoring uses: the event's services plus what they call."""
    services = set(anomaly.services)
    for service in anomaly.services:
        if service in graph:
            services |= set(graph.downstream(service, max_hops))
    return services


class IncidentIndex(Protocol):
    def incident_count(self) -> int: ...

    def search_incidents(
        self, vector: list[float], services: list[str], fault_types: list[str], top_k: int, hybrid: bool
    ) -> list[SimilarIncident]: ...


@dataclass(frozen=True)
class RetrievalResult:
    status: RetrievalStatus
    incidents: list[SimilarIncident] = field(default_factory=list)
    detail: str = ""


class Retriever:
    def __init__(
        self,
        embed: Embed,
        index: IncidentIndex,
        graph: DependencyGraph,
        mode: str = "hybrid",
        top_k: int = 3,
        max_hops: int = DEFAULT_MAX_HOPS,
    ):
        if mode not in ("hybrid", "vector"):
            raise ValueError(f"retrieval mode must be 'hybrid' or 'vector', got {mode!r}")
        if top_k < 1:
            raise ValueError(f"top_k must be >= 1, got {top_k}")
        self._embed = embed
        self._index = index
        self._graph = graph
        self._mode = mode
        self._top_k = top_k
        self._max_hops = max_hops

    def retrieve(self, anomaly: AnomalyEvent) -> RetrievalResult:
        if self._index.incident_count() == 0:
            return RetrievalResult("empty_corpus", detail="no incidents ingested; run corpus/ingest.py")
        try:
            vectors = self._embed([query_text(anomaly)])
        except OllamaUnavailable as exc:
            log.warning("retrieval skipped for %s: %s", anomaly.anomaly_id, exc)
            return RetrievalResult("embedding_unavailable", detail=str(exc))
        if len(vectors) != 1 or len(vectors[0]) != EMBEDDING_DIMENSIONS:
            detail = f"expected one {EMBEDDING_DIMENSIONS}-dim embedding, got sizes {[len(v) for v in vectors]}"
            log.warning("retrieval skipped for %s: %s", anomaly.anomaly_id, detail)
            return RetrievalResult("embedding_unavailable", detail=detail)
        incidents = self._index.search_incidents(
            vectors[0],
            sorted(candidate_services(anomaly, self._graph, self._max_hops)),
            sorted(suspected_fault_types(anomaly.metrics)),
            self._top_k,
            hybrid=self._mode == "hybrid",
        )
        return RetrievalResult("ok", incidents)
