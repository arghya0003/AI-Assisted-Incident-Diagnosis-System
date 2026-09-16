"""What a hypothesis about each candidate may cite and propose, and the internal diagnosis result.

Shared by the LLM prompt and response schema (app/prompts.py), LLM reply validation (app/llm.py)
and the deterministic fallback (app/deterministic.py), so all three obey exactly the same limits.
"""

import math
from dataclasses import dataclass, field

from app.models import NO_ACTION, AnalyzeResponse, Candidate, CandidateReport

MAX_HYPOTHESES = 3
# A deploy scores >= 0.5 when it landed within about 7 minutes of onset. Older deploys can still be
# cited as evidence, but are not offered for rollback.
ROLLBACK_MIN_DEPLOY_PROXIMITY = 0.5
# Evidence categories whose source ids are records a hypothesis may cite
# (CONTRACTS.md: every evidence id resolves to an anomaly, deploy or incident).
CITABLE_CATEGORIES = ("anomaly", "deployment", "similar_incident")


def rollback_window_minutes(deploy_decay_minutes: float) -> float:
    """How recent a deploy must be to be offered for rollback: the age at which its deploy score,
    exp(-minutes / decay), falls to ROLLBACK_MIN_DEPLOY_PROXIMITY (about 7 minutes by default). For the
    llm_only mode, which has no deploy scores but must apply the same rule."""
    return -deploy_decay_minutes * math.log(ROLLBACK_MIN_DEPLOY_PROXIMITY)


@dataclass(frozen=True)
class CandidateOptions:
    service: str
    citable_ids: list[str]  # the anomaly itself first
    actions: list[str]  # no_action first
    # Whether any deploy to this candidate falls in the lookback window. A cause may only talk about a
    # deploy when this is true (app/llm.py).
    has_recent_deploy: bool = False


def candidate_options(report: CandidateReport, candidate: Candidate) -> CandidateOptions:
    """restart_service and scale_service are in the contract's vocabulary but never offered here:
    nothing the scorer measures shows that a service has failed or is overloaded, and when they
    were offered, phi4-mini proposed a restart for every fixture, benign ones included."""
    evidence = {item.evidence_id: item for item in report.evidence}
    citable = list(
        dict.fromkeys(
            evidence[evidence_id].source_id
            for evidence_id in candidate.evidence_ids
            if evidence[evidence_id].category in CITABLE_CATEGORIES
        )
    )
    actions = [NO_ACTION]
    if candidate.deploy_id and candidate.signals.deploy_proximity >= ROLLBACK_MIN_DEPLOY_PROXIMITY:
        actions.append(f"rollback_deploy:{candidate.deploy_id}")
    return CandidateOptions(
        service=candidate.service,
        citable_ids=citable,
        actions=actions,
        has_recent_deploy=candidate.deploy_id is not None,
    )


@dataclass(frozen=True)
class Diagnosis:
    response: AnalyzeResponse  # exactly the contract shape returned to M4
    services: list[str]  # the candidate each hypothesis is about, in rank order (not in the contract)
    adjustments: list[str] = field(default_factory=list)  # normalisations applied to a valid LLM reply
