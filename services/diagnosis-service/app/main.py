"""diagnosis-service HTTP API.

POST /analyze runs the full pipeline: stored anomaly -> retrieval -> deterministic scoring ->
phi4-mini, with a deterministic fallback so the response is always contract-valid. The
X-Diagnosis-Mode header says which produced the answer. GET /candidates/{anomaly_id} exposes the
deterministic ranking that the LLM is given.
"""

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Response

from app.consumer import AnomalyConsumer
from app.db import AnomalyStore, DatabaseUnavailable, PostgresAnomalyStore
from app.graph import load_graph
from app.guardrail import GuardrailStats
from app.llm import Chat
from app.models import AnalyzeRequest, AnalyzeResponse, CandidateReport
from app.ollama import OllamaClient
from app.pipeline import DiagnosisPipeline, PipelineConfig, ollama_chat, ollama_embed
from app.retrieval import Embed
from app.scoring import ScoringConfig
from app.settings import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("diagnosis-service")

SERVICE_VERSION = "0.7.0"
STARTED_AT = datetime.now(timezone.utc)
# The configured pipeline. Phase 8 adds llm_only, no_graph and deterministic for ablations.
PIPELINE_MODE = "full"

# Loaded at import so a broken graph file or invalid settings stop the container at startup,
# not on the first request.
graph = load_graph()
scoring_config = ScoringConfig.from_settings(settings)
pipeline_config = PipelineConfig.from_settings(settings)
store = PostgresAnomalyStore(settings)
consumer = AnomalyConsumer(settings)
ollama = OllamaClient(settings.ollama_url, timeout_seconds=settings.ollama_timeout_seconds)
guardrail_stats = GuardrailStats()


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
        "dependency graph: %d nodes, %d edges; score weights %s; retrieval %s top_k=%d; llm attempts=%d",
        len(graph.nodes),
        len(graph.edges),
        scoring_config.weights,
        settings.retrieval_mode,
        settings.retrieval_top_k,
        settings.llm_max_attempts,
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
    return ollama_embed(ollama, settings)


def get_chat() -> Chat:
    return ollama_chat(ollama, settings)


def _pipeline(anomalies: AnomalyStore, embed: Embed, chat: Chat) -> DiagnosisPipeline:
    return DiagnosisPipeline(anomalies, embed, chat, graph, scoring_config, pipeline_config, guardrail_stats)


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


@app.get("/stats")
def stats() -> dict[str, object]:
    """Evidence-guardrail counters since the service started. They reset on restart."""
    return {"since": STARTED_AT.isoformat(), "guardrail": guardrail_stats.snapshot()}


@app.post("/analyze", response_model=AnalyzeResponse, responses=ERROR_RESPONSES)
def analyze(
    request: AnalyzeRequest,
    response: Response,
    anomalies: AnomalyStore = Depends(get_store),
    embed: Embed = Depends(get_embedder),
    chat: Chat = Depends(get_chat),
) -> AnalyzeResponse:
    try:
        result = _pipeline(anomalies, embed, chat).analyze(request.anomaly_id)
    except DatabaseUnavailable as exc:
        raise _unavailable(request.anomaly_id, exc) from exc
    if result is None:
        raise _not_found(request.anomaly_id)

    attempts = result.llm.attempts if result.llm else 0
    response.headers["X-Diagnosis-Mode"] = result.mode
    response.headers["X-LLM-Attempts"] = str(attempts)
    response.headers["X-Guardrail-Rejected"] = str(len(result.guardrail_rejections))
    log.info(
        "analyze anomaly_id=%s mode=%s attempts=%d latency_ms=%d hypotheses=%d guardrail_rejected=%d%s",
        request.anomaly_id,
        result.mode,
        attempts,
        result.latency_ms,
        len(result.response.hypotheses),
        len(result.guardrail_rejections),
        f" fallback_reason={result.fallback_reason}" if result.fallback_reason else "",
    )
    return result.response


@app.get("/candidates/{anomaly_id}", response_model=CandidateReport, responses=ERROR_RESPONSES)
def candidates(
    anomaly_id: str,
    anomalies: AnomalyStore = Depends(get_store),
    embed: Embed = Depends(get_embedder),
    chat: Chat = Depends(get_chat),
) -> CandidateReport:
    """Debug view of the deterministic ranking: every candidate with its per-signal breakdown,
    the similar past incidents retrieved, and the evidence behind it. Read-only; no LLM
    generation (retrieval embeds the query with nomic-embed-text)."""
    try:
        report = _pipeline(anomalies, embed, chat).candidate_report(anomaly_id)
    except DatabaseUnavailable as exc:
        raise _unavailable(anomaly_id, exc) from exc
    if report is None:
        raise _not_found(anomaly_id)
    return report
