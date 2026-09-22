"""diagnosis-service HTTP API.

POST /analyze runs the pipeline (app/pipeline.py) in the configured mode, or the one requested with
?mode=. It stores the run in the analyses and hypotheses tables, and serves a stored answer again for
a repeat question. Response headers say how each answer was produced. GET /hypotheses/{anomaly_id}
lists stored runs, GET /candidates/{anomaly_id} shows the deterministic ranking, and GET /stats shows
the evidence-guardrail counters.
"""

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, Query, Response

from app.analyses import config_fingerprint, model_version_for, new_analysis_id, stored_analysis
from app.db import AnomalyStore, DatabaseUnavailable, PostgresAnomalyStore
from app.graph import load_graph
from app.guardrail import GuardrailStats
from app.llm import Chat
from app.models import AnalyzeRequest, AnalyzeResponse, CandidateReport, PipelineMode, StoredAnalysis
from app.ollama import OllamaClient
from app.pipeline import DiagnosisPipeline, PipelineConfig, ollama_chat, ollama_embed
from app.retrieval import Embed
from app.scoring import ScoringConfig
from app.seed import seed_corpus_if_empty
from app.settings import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("diagnosis-service")

SERVICE_VERSION = "0.8.0"
STARTED_AT = datetime.now(timezone.utc)

# Loaded at import so a broken graph file or invalid settings stop the container at startup,
# not on the first request.
graph = load_graph()
scoring_config = ScoringConfig.from_settings(settings)
pipeline_config = PipelineConfig.from_settings(settings)
store = PostgresAnomalyStore(settings)
ollama = OllamaClient(settings.ollama_url, timeout_seconds=settings.ollama_timeout_seconds)
guardrail_stats = GuardrailStats()
CONFIG_FINGERPRINT = config_fingerprint(SERVICE_VERSION, settings, scoring_config, pipeline_config)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log.info(
        "starting diagnosis-service %s mode=%s llm=%s embed=%s ollama=%s num_ctx=%d fingerprint=%s",
        SERVICE_VERSION,
        settings.pipeline_mode,
        settings.llm_model,
        settings.embed_model,
        settings.ollama_url,
        settings.llm_context_tokens,
        CONFIG_FINGERPRINT,
    )
    seed_corpus_if_empty(store, settings.embed_model)
    log.info(
        "dependency graph: %d nodes, %d edges; score weights %s; retrieval %s top_k=%d; llm attempts=%d",
        len(graph.nodes),
        len(graph.edges),
        scoring_config.weights,
        settings.retrieval_mode,
        settings.retrieval_top_k,
        settings.llm_max_attempts,
    )
    yield


app = FastAPI(
    title="diagnosis-service",
    version=SERVICE_VERSION,
    description="M3: ranked, evidence-cited root-cause hypotheses for an anomaly.",
    lifespan=lifespan,
)

ERROR_RESPONSES = {
    404: {"description": "No anomaly with this id has been received"},
    503: {"description": "TimescaleDB unreachable or its schema out of date"},
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


def _describe(response: Response, analysis: StoredAnalysis, cache: str, persisted: bool) -> None:
    response.headers["X-Diagnosis-Mode"] = analysis.answered_by
    response.headers["X-Pipeline-Mode"] = analysis.pipeline_mode
    response.headers["X-LLM-Attempts"] = str(analysis.llm_attempts)
    response.headers["X-Guardrail-Rejected"] = str(analysis.guardrail_rejected)
    response.headers["X-Analysis-Id"] = analysis.analysis_id
    response.headers["X-Cache"] = cache
    response.headers["X-Persisted"] = "true" if persisted else "false"


@app.get("/health")
def health(anomalies: AnomalyStore = Depends(get_store)) -> dict[str, object]:
    # Always 200 while the process is up: a database outage is reported here, not turned
    # into container restarts that couldn't fix it.
    #
    # corpus_incidents is here because an empty corpus is otherwise invisible (issue #19):
    # retrieval returns nothing, incident similarity contributes 0 to every score, and the
    # service still answers, so a demo or evaluation on a fresh volume can silently measure a
    # system with the "retrieval-augmented" half switched off. null means the database could
    # not be reached to count them.
    try:
        corpus_incidents: int | None = anomalies.incident_count()
    except DatabaseUnavailable:
        corpus_incidents = None
    return {
        "status": "ok",
        "service": "diagnosis-service",
        "version": SERVICE_VERSION,
        "pipeline_mode": settings.pipeline_mode,
        "config_fingerprint": CONFIG_FINGERPRINT,
        "database": anomalies.status(),
        "corpus_incidents": corpus_incidents,
        "retrieval": "ready" if corpus_incidents else "empty_corpus: run scripts/test_in_docker.sh --ingest",
    }


@app.get("/stats")
def stats() -> dict[str, object]:
    """Evidence-guardrail counters since the service started. They reset on restart."""
    return {"since": STARTED_AT.isoformat(), "guardrail": guardrail_stats.snapshot()}


@app.post("/analyze", response_model=AnalyzeResponse, responses=ERROR_RESPONSES)
def analyze(
    request: AnalyzeRequest,
    response: Response,
    mode: PipelineMode | None = Query(None, description="Pipeline mode; defaults to the PIPELINE_MODE setting"),
    refresh: bool = Query(False, description="Ignore any stored answer and run the pipeline again"),
    anomalies: AnomalyStore = Depends(get_store),
    embed: Embed = Depends(get_embedder),
    chat: Chat = Depends(get_chat),
) -> AnalyzeResponse:
    mode = mode or settings.pipeline_mode
    model_version = model_version_for(mode, settings)
    pipeline = _pipeline(anomalies, embed, chat)
    try:
        inputs = pipeline.scoring_inputs(request.anomaly_id)
        if inputs is None:
            raise _not_found(request.anomaly_id)
        if not refresh:
            cached = anomalies.latest_reusable_analysis(request.anomaly_id, mode, model_version, CONFIG_FINGERPRINT)
            # A run made before the co-anomaly window closed may have missed related anomalies that
            # arrived later, so it is not served again.
            inputs_complete_at = inputs.anomaly.t_onset + timedelta(seconds=scoring_config.co_anomaly_window_seconds)
            if cached is not None and cached.created_at is not None and cached.created_at >= inputs_complete_at:
                _describe(response, cached, cache="hit", persisted=True)
                log.info("analyze anomaly_id=%s mode=%s served stored analysis %s", request.anomaly_id, mode, cached.analysis_id)
                return cached.response()
        result = pipeline.analyze_inputs(inputs, mode)
    except DatabaseUnavailable as exc:
        raise _unavailable(request.anomaly_id, exc) from exc

    analysis = stored_analysis(result, new_analysis_id(), request.anomaly_id, model_version, CONFIG_FINGERPRINT)
    persisted = True
    try:
        anomalies.save_analysis(analysis, result.report.evidence if result.report else [])
    except DatabaseUnavailable as exc:
        # The answer is still valid; failing the request would lose it too.
        persisted = False
        log.error("analysis %s for %s was not stored: %s", analysis.analysis_id, request.anomaly_id, exc)

    _describe(response, analysis, cache="miss", persisted=persisted)
    log.info(
        "analyze anomaly_id=%s mode=%s answered_by=%s attempts=%d latency_ms=%d hypotheses=%d guardrail_rejected=%d%s",
        request.anomaly_id,
        mode,
        result.mode,
        analysis.llm_attempts,
        result.latency_ms,
        len(result.response.hypotheses),
        analysis.guardrail_rejected,
        f" fallback_reason={result.fallback_reason}" if result.fallback_reason else "",
    )
    return result.response


@app.get("/hypotheses/{anomaly_id}", response_model=list[StoredAnalysis], responses=ERROR_RESPONSES)
def hypotheses(
    anomaly_id: str,
    mode: PipelineMode | None = Query(None, description="Only runs in this pipeline mode"),
    limit: int = Query(20, ge=1, le=200),
    anomalies: AnomalyStore = Depends(get_store),
) -> list[StoredAnalysis]:
    """Stored /analyze runs for an anomaly, newest first, each with its hypotheses. For M4 and for
    M2's evaluation runner."""
    try:
        if anomalies.get(anomaly_id) is None:
            raise _not_found(anomaly_id)
        return anomalies.analyses(anomaly_id, mode, limit)
    except DatabaseUnavailable as exc:
        raise _unavailable(anomaly_id, exc) from exc


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
