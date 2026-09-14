"""Hypotheses from the deterministic ranking alone, with templated causes.

Used when the LLM cannot produce a valid response, so /analyze always returns a contract-valid
answer. Phase 8's `deterministic` pipeline mode will use the same function as the no-LLM baseline.
It follows the same per-candidate limits as the LLM (app/hypotheses.py): it cites only that
candidate's evidence, and proposes a rollback exactly when one is offered.
"""

from app.hypotheses import MAX_HYPOTHESES, Diagnosis, candidate_options
from app.models import AnalyzeResponse, CandidateReport, Hypothesis

CAUSE_PREFIX = "Deterministic ranking (LLM not used): "


def deterministic_diagnosis(report: CandidateReport, limit: int = MAX_HYPOTHESES) -> Diagnosis:
    evidence = {item.evidence_id: item for item in report.evidence}
    hypotheses, services = [], []
    for rank, candidate in enumerate(report.candidates[:limit], start=1):
        options = candidate_options(report, candidate)
        items = [evidence[evidence_id] for evidence_id in candidate.evidence_ids]
        reasons = [
            item.summary
            for item in items
            if item.category in ("deployment", "dependency", "similar_incident")
            or (item.category == "anomaly" and item.source_id != report.anomaly_id)
        ]
        if candidate.signals.co_anomaly:
            reasons.append("it is the deepest anomalous service")
        detail = "; ".join(reasons) if reasons else "no supporting signal beyond being on the anomalous path"
        hypotheses.append(
            Hypothesis(
                rank=rank,
                cause=f"{CAUSE_PREFIX}{candidate.service} scored {candidate.score:.2f}: {detail}.",
                confidence=round(candidate.score, 3),
                evidence_ids=options.citable_ids,
                # The rollback when one is offered (a deploy shortly before onset), otherwise no_action.
                proposed_action=options.actions[-1],
            )
        )
        services.append(candidate.service)
    return Diagnosis(response=AnalyzeResponse(hypotheses=hypotheses), services=services)
