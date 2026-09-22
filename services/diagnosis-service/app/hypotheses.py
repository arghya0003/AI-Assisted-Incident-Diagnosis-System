"""What a hypothesis about each candidate may cite and propose, and the internal diagnosis result.

Shared by the LLM prompt and response schema (app/prompts.py), LLM reply validation (app/llm.py)
and the deterministic fallback (app/deterministic.py), so all three obey exactly the same limits.
"""

import math
from dataclasses import dataclass, field

from app.models import LIVENESS_METRIC, NO_ACTION, AnalyzeResponse, Candidate, CandidateReport, Evidence

MAX_HYPOTHESES = 3
# A deploy scores >= 0.5 when it landed within about 7 minutes of onset. Older deploys can still be
# cited as evidence, but are not offered for rollback.
ROLLBACK_MIN_DEPLOY_PROXIMITY = 0.5
# Evidence categories whose source ids are records a hypothesis may cite
# (CONTRACTS.md: every evidence id resolves to an anomaly, deploy or incident).
CITABLE_CATEGORIES = ("anomaly", "deployment", "similar_incident")
# M2's detector name for "this service stopped publishing metrics at all". A restart is offered
# only on this evidence, so the offer follows the signal rather than the model's imagination.
STALENESS_DETECTOR = "staleness"


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


def reported_silent(evidence: dict[str, Evidence], candidate: Candidate) -> bool:
    """True when a staleness anomaly cited by this candidate says this service stopped reporting
    metrics altogether - M2's evidence that it has failed, rather than an inference about it."""
    for evidence_id in candidate.evidence_ids:
        item = evidence.get(evidence_id)
        if item is None or item.category != "anomaly":
            continue
        payload = item.payload
        if (
            payload.get("detector") == STALENESS_DETECTOR
            and LIVENESS_METRIC in (payload.get("metrics") or [])
            and candidate.service in (payload.get("services") or [])
        ):
            return True
    return False


def candidate_options(report: CandidateReport, candidate: Candidate) -> CandidateOptions:
    """Which of the contract's four verbs this candidate may be proposed for.

    `restart_service` is offered only to a service M2's staleness detector reports as silent
    (issue #23). That signal did not exist when this module was written, and an ungated restart
    option was actively harmful: phi4-mini proposed a restart for every fixture, benign ones
    included. Gating it on the evidence keeps that impossible - the option is absent unless a
    `liveness` anomaly names this service - while making the textbook `service_crash` case
    actionable instead of `no_action` on a service that is down.

    `scale_service` stays unreachable: nothing measured here shows a service is overloaded.
    Offering it would be guessing, and it is left validated but unused until a signal exists."""
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
    if reported_silent(evidence, candidate):
        actions.append(f"restart_service:{candidate.service}")
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
