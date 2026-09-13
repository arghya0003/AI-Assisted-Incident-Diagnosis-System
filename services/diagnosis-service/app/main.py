"""diagnosis-service HTTP API.

Phase 1: /analyze is a contract-shaped stub so M4 can build against the real endpoint and
schema now. Later phases replace the body of analyze() with the scoring -> retrieval ->
LLM -> guardrail pipeline without changing the request or response shape.
"""

import logging

from fastapi import FastAPI, Response

from app.models import NO_ACTION, AnalyzeRequest, AnalyzeResponse, Hypothesis
from app.settings import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("diagnosis-service")

SERVICE_VERSION = "0.1.0"
# Returned in /health and the X-Diagnosis-Mode header so a stub response is never
# mistaken for a real diagnosis. Becomes full/llm_only/no_graph/deterministic in Phase 8.
PIPELINE_MODE = "stub"

app = FastAPI(
    title="diagnosis-service",
    version=SERVICE_VERSION,
    description="M3: ranked, evidence-cited root-cause hypotheses for an anomaly.",
)

log.info(
    "starting diagnosis-service %s mode=%s llm=%s embed=%s ollama=%s num_ctx=%d",
    SERVICE_VERSION,
    PIPELINE_MODE,
    settings.llm_model,
    settings.embed_model,
    settings.ollama_url,
    settings.llm_context_tokens,
)


@app.get("/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "diagnosis-service",
        "version": SERVICE_VERSION,
        "pipeline_mode": PIPELINE_MODE,
    }


@app.post("/analyze", response_model=AnalyzeResponse)
def analyze(request: AnalyzeRequest, response: Response) -> AnalyzeResponse:
    # Stub: anomalies are not persisted until Phase 2, so any well-formed id gets this
    # placeholder. It cites only the requested anomaly_id and proposes no action, so it
    # never points a human at evidence or a remediation that doesn't exist.
    response.headers["X-Diagnosis-Mode"] = PIPELINE_MODE
    log.info("analyze anomaly_id=%s mode=%s", request.anomaly_id, PIPELINE_MODE)
    return AnalyzeResponse(
        hypotheses=[
            Hypothesis(
                rank=1,
                cause="[stub] Diagnosis pipeline not implemented yet; "
                "this placeholder only demonstrates the /analyze response shape.",
                confidence=0.0,
                evidence_ids=[request.anomaly_id],
                proposed_action=NO_ACTION,
            )
        ]
    )
