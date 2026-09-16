"""Identifying and recording /analyze runs (Phase 8).

A stored run is keyed for the response cache by anomaly, pipeline mode, model version and a
fingerprint of everything that shapes an answer. A changed prompt, weight or model setting therefore
never serves an answer produced under the old one.
"""

import dataclasses
import hashlib
import json
import uuid
from pathlib import Path

from app.corpus import CORPUS_DIR
from app.graph import DEFAULT_GRAPH_PATH
from app.models import PipelineMode, StoredAnalysis, StoredHypothesis
from app.pipeline import PipelineConfig, PipelineResult
from app.prompts import PROMPTS_DIR
from app.scoring import ScoringConfig
from app.settings import Settings

NO_MODEL = "none"


def _file_digest(*paths: Path) -> str:
    """A stable digest of file contents, so an edited input changes the fingerprint."""
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes() if path.is_file() else b"")
    return digest.hexdigest()[:16]


def new_analysis_id() -> str:
    return f"an-{uuid.uuid4().hex[:16]}"


def model_version_for(mode: PipelineMode, settings: Settings) -> str:
    return NO_MODEL if mode == "deterministic" else settings.llm_model


def config_fingerprint(service_version: str, settings: Settings, scoring: ScoringConfig, pipeline: PipelineConfig) -> str:
    material = {
        "service_version": service_version,
        "llm": {
            "model": settings.llm_model,
            "temperature": settings.llm_temperature,
            "max_output_tokens": settings.llm_max_output_tokens,
            "context_tokens": settings.llm_context_tokens,
            "max_attempts": settings.llm_max_attempts,
        },
        "embed_model": settings.embed_model,
        "scoring": dataclasses.asdict(scoring),
        "pipeline": dataclasses.asdict(pipeline),
        "prompts": {path.name: path.read_text(encoding="utf-8") for path in sorted(PROMPTS_DIR.glob("*.txt"))},
        # The graph decides candidates and their distances; the corpus decides what retrieval can
        # return. Both change the answer, so both belong in the key or a stored run made before an
        # edit would be served again after it.
        "graph": _file_digest(DEFAULT_GRAPH_PATH),
        "corpus": _file_digest(*sorted(CORPUS_DIR.glob("incident-*.md"))),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def stored_analysis(
    result: PipelineResult, analysis_id: str, anomaly_id: str, model_version: str, fingerprint: str
) -> StoredAnalysis:
    return StoredAnalysis(
        analysis_id=analysis_id,
        anomaly_id=anomaly_id,
        pipeline_mode=result.pipeline_mode,
        answered_by=result.mode,
        model_version=model_version,
        config_fingerprint=fingerprint,
        llm_attempts=result.llm.attempts if result.llm else 0,
        guardrail_rejected=len(result.guardrail_rejections),
        latency_ms=result.latency_ms,
        fallback_reason=result.fallback_reason,
        hypotheses=[
            StoredHypothesis(
                rank=hypothesis.rank,
                service=service,
                cause=hypothesis.cause,
                confidence=hypothesis.confidence,
                evidence_ids=hypothesis.evidence_ids,
                proposed_action=hypothesis.proposed_action,
            )
            for hypothesis, service in zip(result.response.hypotheses, result.services)
        ],
    )
