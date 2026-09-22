"""Wire shapes for the orchestrator (M4).

Everything an incident carries end to end: the anomaly it was opened for, M3's hypotheses
verbatim (CONTRACTS.md's `AnalyzeResponse`), the state machine, and the decision an operator
made. The action vocabulary here must match `services/diagnosis-service/app/models.py`
exactly -- that is the interface freeze CONTRACTS.md calls for (the open question is resolved
below, not re-litigated).
"""

import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

NonBlankId = Annotated[str, Field(min_length=1, pattern=r"^\S+$")]

# Fixed, enumerated action vocabulary (CONTRACTS.md open question, resolved with M3's
# proposed_action shape: "<action>:<target_id>", or the bare "no_action"). Never free text --
# an operator approves one of these four verbs, nothing else can reach the executor.
ACTIONS_WITH_TARGET = ("rollback_deploy", "restart_service", "scale_service")
NO_ACTION = "no_action"
_ACTION_RE = re.compile(r"^(?:(?:" + "|".join(ACTIONS_WITH_TARGET) + r"):\S+|" + NO_ACTION + r")$")

# Each action's blast radius, shown to the operator before they approve it and recorded on the
# audit row. "single-service" / "none" are the only radii this system can ever propose --
# nothing here can name more than one target, by construction of the action grammar above.
ACTION_BLAST_RADIUS: dict[str, str] = {
    "rollback_deploy": "single-service (reverts one deploy)",
    "restart_service": "single-service (brief availability gap)",
    "scale_service": "single-service (resource change only)",
    "no_action": "none",
}


def action_verb(proposed_action: str) -> str:
    return proposed_action.split(":", 1)[0]


def parse_action(proposed_action: str) -> tuple[str, str | None]:
    """Split `verb:target` into its parts, or return (`no_action`, None)."""
    if ":" not in proposed_action:
        return proposed_action, None
    verb, target = proposed_action.split(":", 1)
    return verb, target


def action_is_valid(proposed_action: str) -> bool:
    return bool(_ACTION_RE.match(proposed_action))


class IncidentState(StrEnum):
    """DETECTED -> ANALYZING -> AWAITING_APPROVAL -> APPROVED | REJECTED | EXPIRED, plus
    ANALYSIS_FAILED when M3 could not be reached or returned nothing usable after retries --
    a dead end the orchestrator can leave, not one it gets stuck in (PLAN.md: "handles ...
    the case where the LLM is slow or returns garbage")."""

    DETECTED = "DETECTED"
    ANALYZING = "ANALYZING"
    ANALYSIS_FAILED = "ANALYSIS_FAILED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


TERMINAL_STATES = (IncidentState.APPROVED, IncidentState.REJECTED, IncidentState.EXPIRED)


class Hypothesis(BaseModel):
    """One of M3's ranked hypotheses, as stored on the incident. Mirrors
    diagnosis-service's own `Hypothesis` model; kept as a separate copy so a change on
    either side of the frozen `POST /analyze` contract fails loudly instead of silently."""

    model_config = ConfigDict(extra="ignore")

    rank: int = Field(ge=1)
    cause: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_ids: list[NonBlankId] = Field(min_length=1)
    proposed_action: str

    @field_validator("proposed_action")
    @classmethod
    def _action_in_vocabulary(cls, value: str) -> str:
        if not action_is_valid(value):
            allowed = ", ".join(f"{a}:<target_id>" for a in ACTIONS_WITH_TARGET)
            raise ValueError(f"must be one of {allowed}, or {NO_ACTION}; got {value!r}")
        return value

    @property
    def blast_radius(self) -> str:
        return ACTION_BLAST_RADIUS.get(action_verb(self.proposed_action), "unknown")


class AnomalyEvent(BaseModel):
    """An `anomalies.detected` record (CONTRACTS.md). M2 owns the shape; unknown fields are
    ignored so an additive change on their side never breaks ingestion here."""

    model_config = ConfigDict(extra="ignore")

    anomaly_id: NonBlankId
    services: list[str] = Field(min_length=1)
    metrics: list[str] = Field(min_length=1)
    severity: str
    t_detected: datetime
    t_onset: datetime
    evidence_window: dict[str, Any] | None = None
    detector: str | None = None
    in_deploy_window: bool | None = None
    related_deploy_ids: list[str] = []
    contributors: list[dict[str, Any]] = []


class Incident(BaseModel):
    """One row of the `incidents` table (timescaledb/init/008_incidents.sql)."""

    model_config = ConfigDict(extra="ignore")

    incident_id: str
    anomaly_id: str
    state: IncidentState
    services: list[str]
    severity: str
    anomaly: dict[str, Any]
    analysis_id: str | None = None
    model_version: str | None = None
    answered_by: str | None = None
    hypotheses: list[Hypothesis] = []
    analysis_attempts: int = 0
    fail_reason: str | None = None
    decision: Literal["approved", "rejected"] | None = None
    decided_hypothesis_rank: int | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_reason: str | None = None
    execution_logged: bool = False
    created_at: datetime
    updated_at: datetime
    awaiting_since: datetime | None = None
    expires_at: datetime | None = None


class ApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hypothesis_rank: int = Field(ge=1)
    approver: str = Field(min_length=1)
    note: str | None = None


class RejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approver: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    hypothesis_rank: int | None = Field(default=None, ge=1)
    # Coarse category for the rejection-feedback table, so it can be aggregated without NLP.
    reason_category: Literal[
        "wrong_root_cause", "wrong_action", "insufficient_evidence", "duplicate", "other"
    ] = "other"


class RequestInfoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approver: str = Field(min_length=1)
    note: str = Field(min_length=1)


class AuditEntry(BaseModel):
    """One row of the immutable `audit_log` table. `hash` chains to `prev_hash`
    (`app/audit.py`), so the sequence is tamper-evident even though nothing in the schema
    stops a superuser from editing rows out-of-band -- that is a Postgres-level guarantee,
    not a cryptographic one; see app/audit.py for the threat model this covers."""

    model_config = ConfigDict(extra="ignore")

    audit_id: int
    incident_id: str | None
    event_type: str
    actor: str
    detail: dict[str, Any]
    prev_hash: str
    hash: str
    created_at: datetime
