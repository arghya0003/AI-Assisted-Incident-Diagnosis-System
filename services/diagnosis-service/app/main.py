"""diagnosis-service HTTP API.

Phase 2: /analyze resolves the anomaly from the anomalies table (fed by the Kafka consumer)
and 404s unknown ids, but the hypothesis is still a stub. Later phases replace the stub with
the scoring -> retrieval -> LLM -> guardrail pipeline without changing the request or
response shape.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Response

from app.consumer import AnomalyConsumer
from app.db import AnomalyStore, DatabaseUnavailable, PostgresAnomalyStore
from app.models import NO_ACTION, AnalyzeRequest, AnalyzeResponse, Hypothesis
from app.settings import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("diagnosis-service")

SERVICE_VERSION = "0.2.0"
# Returned in /health and the X-Diagnosis-Mode header so a stub response is never
# mistaken for a real diagnosis. Becomes full/llm_only/no_graph/deterministic in Phase 8.
PIPELINE_MODE = "stub"

store = PostgresAnomalyStore(settings)
consumer = AnomalyConsumer(settings)


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


def get_store() -> AnomalyStore:
    return store


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


@app.post(
    "/analyze",
    response_model=AnalyzeResponse,
    responses={
        404: {"description": "No anomaly with this id has been received"},
        503: {"description": "TimescaleDB unreachable or migration not applied"},
    },
)
def analyze(
    request: AnalyzeRequest,
    response: Response,
    anomalies: AnomalyStore = Depends(get_store),
) -> AnalyzeResponse:
    try:
        anomaly = anomalies.get(request.anomaly_id)
    except DatabaseUnavailable as exc:
        log.error("analyze anomaly_id=%s: %s", request.anomaly_id, exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if anomaly is None:
        raise HTTPException(status_code=404, detail=f"unknown anomaly_id {request.anomaly_id!r}")

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
