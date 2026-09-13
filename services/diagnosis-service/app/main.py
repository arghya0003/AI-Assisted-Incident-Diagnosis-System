"""diagnosis-service HTTP API.

Phase 5: /analyze resolves the anomaly from the anomalies table (fed by the Kafka consumer)
and 404s unknown ids, but the hypothesis is still a stub. GET /candidates/{anomaly_id} exposes
the deterministic candidate ranking, including similar past incidents from retrieval, that
later phases hand to the LLM. Later phases replace the stub with the scoring -> retrieval ->
LLM -> guardrail pipeline without changing the request or response shape.
"""

import dataclasses
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Response

from app.consumer import AnomalyConsumer
from app.db import AnomalyStore, DatabaseUnavailable, PostgresAnomalyStore
from app.graph import load_graph
from app.models import NO_ACTION, AnalyzeRequest, AnalyzeResponse, CandidateReport, Hypothesis
from app.ollama import OllamaClient
from app.retrieval import Embed, Retriever
from app.scoring import ScoringConfig, score_candidates
from app.settings import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("diagnosis-service")

SERVICE_VERSION = "0.5.0"
# Returned in /health and the X-Diagnosis-Mode header so a stub response is never
# mistaken for a real diagnosis. Becomes full/llm_only/no_graph/deterministic in Phase 8.
PIPELINE_MODE = "stub"

# Loaded at import so a broken graph file or invalid settings stop the container at startup,
# not on the first request.
graph = load_graph()
scoring_config = ScoringConfig.from_settings(settings)
store = PostgresAnomalyStore(settings)
consumer = AnomalyConsumer(settings)
ollama = OllamaClient(settings.ollama_url, timeout_seconds=settings.ollama_timeout_seconds)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log.info(
        "starting diagnosis-service %s mode=%s llm=%s embed=%s ollama=%s num_ctx=%d consumer=%s",
        SERVICE_VERSION,
        PIPELINE_MODE,
        settings.llm_model,
        settings.embed_model,
        settings.ollama_url,
        settings.llm_context_tokens,
        settings.consumer_enabled,
    )
    log.info(
        "dependency graph: %d nodes, %d edges; score weights %s; retrieval %s top_k=%d",
        len(graph.nodes),
        len(graph.edges),
        scoring_config.weights,
        settings.retrieval_mode,
        settings.retrieval_top_k,
    )
    if settings.consumer_enabled:
        consumer.start()
    yield
    consumer.stop()


app = FastAPI(
    title="diagnosis-service",
    version=SERVICE_VERSION,
    description="M3: ranked, evidence-cited root-cause hypotheses for an anomaly.",
    lifespan=lifespan,
)

ERROR_RESPONSES = {
    404: {"description": "No anomaly with this id has been received"},
    503: {"description": "TimescaleDB unreachable or a required table missing"},
}


def get_store() -> AnomalyStore:
    return store


def get_embedder() -> Embed:
    return lambda texts: ollama.embed(texts, settings.embed_model)


def _unavailable(anomaly_id: str, exc: DatabaseUnavailable) -> HTTPException:
    log.error("anomaly_id=%s: %s", anomaly_id, exc)
    return HTTPException(status_code=503, detail=str(exc))


def _not_found(anomaly_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"unknown anomaly_id {anomaly_id!r}")


@app.get("/health")
def health(anomalies: AnomalyStore = Depends(get_store)) -> dict[str, str]:
    # Always 200 while the process is up: a database outage is reported here, not turned
    # into container restarts that couldn't fix it.
    return {
        "status": "ok",
        "service": "diagnosis-service",
        "version": SERVICE_VERSION,
        "pipeline_mode": PIPELINE_MODE,
        "database": anomalies.status(),
        "consumer": consumer.state if settings.consumer_enabled else "disabled",
    }


@app.post("/analyze", response_model=AnalyzeResponse, responses=ERROR_RESPONSES)
def analyze(
    request: AnalyzeRequest,
    response: Response,
    anomalies: AnomalyStore = Depends(get_store),
) -> AnalyzeResponse:
    try:
        anomaly = anomalies.get(request.anomaly_id)
    except DatabaseUnavailable as exc:
        raise _unavailable(request.anomaly_id, exc) from exc
    if anomaly is None:
        raise _not_found(request.anomaly_id)

    response.headers["X-Diagnosis-Mode"] = PIPELINE_MODE
    log.info("analyze anomaly_id=%s mode=%s", anomaly.anomaly_id, PIPELINE_MODE)
    # Still a stub: it cites only the anomaly itself, which is now a real stored record,
    # and proposes no action.
    return AnalyzeResponse(
        hypotheses=[
            Hypothesis(
                rank=1,
                cause=f"[stub] Anomaly on {', '.join(anomaly.services)} "
                f"({', '.join(anomaly.metrics)}) was found, but the diagnosis pipeline "
                "is not implemented yet.",
                confidence=0.0,
                evidence_ids=[anomaly.anomaly_id],
                proposed_action=NO_ACTION,
            )
        ]
    )


@app.get("/candidates/{anomaly_id}", response_model=CandidateReport, responses=ERROR_RESPONSES)
def candidates(
    anomaly_id: str,
    anomalies: AnomalyStore = Depends(get_store),
    embed: Embed = Depends(get_embedder),
) -> CandidateReport:
    """Debug view of the deterministic ranking: every candidate with its per-signal breakdown,
    the similar past incidents retrieved, and the evidence behind it. Read-only; no LLM
    generation (retrieval embeds the query with nomic-embed-text)."""
    try:
        inputs = anomalies.scoring_inputs(
            anomaly_id, scoring_config.co_anomaly_window_seconds, scoring_config.deploy_lookback_minutes
        )
        if inputs is None:
            raise _not_found(anomaly_id)
        retriever = Retriever(
            embed, anomalies, graph, mode=settings.retrieval_mode, top_k=settings.retrieval_top_k
        )
        retrieval = retriever.retrieve(inputs.anomaly)
    except DatabaseUnavailable as exc:
        raise _unavailable(anomaly_id, exc) from exc
    inputs = dataclasses.replace(
        inputs, similar_incidents=retrieval.incidents, retrieval_status=retrieval.status
    )
    return score_candidates(inputs, graph, scoring_config)
