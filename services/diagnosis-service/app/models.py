"""Wire shapes from CONTRACTS.md.

These models are the contract. FastAPI validates every /analyze response against them, and
Phase 6 will validate LLM output against the same classes, so shape drift fails here rather
than in M4's UI.
"""

import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Remediation vocabulary (CONTRACTS.md open question: confirm with M4). Formatted as
# "<action>:<target_id>", or the bare "no_action". Never free text.
ACTIONS_WITH_TARGET = ("rollback_deploy", "restart_service", "scale_service")
NO_ACTION = "no_action"
_ACTION_RE = re.compile(
    r"^(?:(?:" + "|".join(ACTIONS_WITH_TARGET) + r"):\S+|" + NO_ACTION + r")$"
)

NonBlankId = Annotated[str, Field(min_length=1, pattern=r"^\S+$")]


class EvidenceWindow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    start: datetime
    end: datetime


class AnomalyEvent(BaseModel):
    """An `anomalies.detected` record. M2 owns this shape, so unknown fields are ignored
    rather than rejected: an additive change on their side must not break ingestion."""

    model_config = ConfigDict(extra="ignore")

    anomaly_id: NonBlankId
    services: list[str] = Field(min_length=1)
    metrics: list[str] = Field(min_length=1)
    severity: str  # enum not yet agreed with M2; the detector currently emits high/medium
    t_detected: datetime
    t_onset: datetime
    evidence_window: EvidenceWindow


class AnalyzeRequest(BaseModel):
    """Body of POST /analyze. Extra fields are rejected so contract drift from M4 is loud."""

    model_config = ConfigDict(extra="forbid")

    anomaly_id: NonBlankId


class Hypothesis(BaseModel):
    # Forbid extras: an LLM that adds fields (e.g. "reasoning") is not following the schema.
    model_config = ConfigDict(extra="forbid")

    rank: int = Field(ge=1)
    cause: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_ids: list[NonBlankId] = Field(min_length=1)
    proposed_action: str

    @field_validator("proposed_action")
    @classmethod
    def _action_in_vocabulary(cls, value: str) -> str:
        if not _ACTION_RE.match(value):
            allowed = ", ".join(f"{a}:<target_id>" for a in ACTIONS_WITH_TARGET)
            raise ValueError(f"must be one of {allowed}, or {NO_ACTION}; got {value!r}")
        return value


class AnalyzeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hypotheses: list[Hypothesis]

    @model_validator(mode="after")
    def _ranks_are_one_to_n(self) -> "AnalyzeResponse":
        ranks = sorted(h.rank for h in self.hypotheses)
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError(f"ranks must be 1..n with no gaps or duplicates; got {ranks}")
        return self


class Deploy(BaseModel):
    """A row of M1's `deploys` table (timescaledb/init/002_deploys.sql)."""

    model_config = ConfigDict(extra="ignore")

    deploy_id: NonBlankId
    service: str
    version: str
    commit_sha: str
    config_diff: str | None = None
    time: datetime


EvidenceCategory = Literal["anomaly", "metrics", "deployment", "dependency", "similar_incident"]


class Evidence(BaseModel):
    """One evidence item, shaped like a row of the `evidence` table (CONTRACTS.md,
    timescaledb/init/004_evidence.sql). `incident_id` is the anomaly being diagnosed."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: NonBlankId
    incident_id: NonBlankId
    category: EvidenceCategory
    source_id: NonBlankId
    service: str | None
    observed_at: datetime
    relevance: float = Field(ge=0.0, le=1.0)
    summary: str = Field(min_length=1)
    payload: dict[str, Any] = {}


class Signals(BaseModel):
    """Per-signal scores for one candidate, each in 0..1, before weighting."""

    model_config = ConfigDict(extra="forbid")

    deploy_proximity: float = Field(ge=0.0, le=1.0)
    graph_proximity: float = Field(ge=0.0, le=1.0)
    co_anomaly: float = Field(ge=0.0, le=1.0)
    incident_similarity: float = Field(ge=0.0, le=1.0)


class SimilarIncident(BaseModel):
    """A past incident returned by retrieval, with its cosine similarity to the anomaly."""

    model_config = ConfigDict(extra="forbid")

    incident_id: NonBlankId
    title: str
    services: list[str]  # where the root cause was
    fault_type: str | None
    source: str | None
    similarity: float = Field(ge=-1.0, le=1.0)
    # From the incident body, for the LLM prompt.
    root_cause: str = ""
    resolution: str = ""


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rank: int = Field(ge=1)
    service: str
    kind: str  # dependency-graph node kind, or "unknown" for a service not in the graph
    score: float = Field(ge=0.0, le=1.0)
    signals: Signals
    distance: int = Field(ge=0)  # hops below the nearest anomalous service in the event
    deploy_id: str | None
    evidence_ids: list[NonBlankId]


class CandidateReport(BaseModel):
    """GET /candidates/{anomaly_id}: the deterministic ranking with everything behind it."""

    model_config = ConfigDict(extra="forbid")

    anomaly_id: str
    t_onset: datetime
    anomalous_services: list[str]  # this event's services plus those of related anomalies
    related_anomaly_ids: list[str]
    weights: dict[str, float]
    # not_run | ok | empty_corpus | embedding_unavailable (app/retrieval.py)
    retrieval_status: str
    similar_incidents: list[SimilarIncident]
    candidates: list[Candidate]
    evidence: list[Evidence]
