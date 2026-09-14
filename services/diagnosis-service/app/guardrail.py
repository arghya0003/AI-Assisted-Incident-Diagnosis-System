"""Evidence guardrail: the hallucination filter. Pure set membership; nothing clever.

Before any hypothesis leaves the service, every entry of its evidence_ids must be in the allowed set:
the anomaly itself, the evidence ids scoring produced for it, and the anomaly, deploy and incident ids
that were actually placed in the prompt. A hypothesis citing anything else is dropped entirely: not
repaired, not returned with a warning. If every LLM hypothesis is dropped, the pipeline returns the
deterministic ranking instead (app/pipeline.py).

With Ollama, the response schema already limits evidence ids while decoding, so this filter should
never fire on phi4-mini's output. It is the enforced backstop for everything the schema can't
guarantee: a provider that doesn't enforce schemas, a schema bug, or a future code path.
"""

import logging
import threading
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

from app.hypotheses import CITABLE_CATEGORIES, Diagnosis
from app.models import AnalyzeResponse, CandidateReport
from app.prompts import Prompt

log = logging.getLogger("diagnosis-service.guardrail")

Source = Literal["llm", "deterministic"]


@dataclass(frozen=True)
class Rejection:
    rank: int  # the hypothesis's rank before filtering
    service: str
    unknown_ids: list[str]


@dataclass(frozen=True)
class GuardrailResult:
    diagnosis: Diagnosis  # the surviving hypotheses, re-ranked 1..n
    checked: int
    rejections: list[Rejection] = field(default_factory=list)


def allowed_ids(anomaly_id: str, report: CandidateReport | None, prompt: Prompt | None) -> frozenset[str]:
    """`report` is None in llm_only mode, which does no scoring; `prompt` is None when no LLM prompt was
    built (deterministic mode, or a prompt over budget)."""
    ids = {anomaly_id}
    if report is not None:
        ids |= {item.evidence_id for item in report.evidence}
    if prompt is not None:
        ids |= set(prompt.citable_ids)
    elif report is not None:
        # Only the deterministic ranking can be answering; it may cite the records behind the
        # report's own evidence.
        ids |= {item.source_id for item in report.evidence if item.category in CITABLE_CATEGORIES}
    return frozenset(ids)


def apply_guardrail(diagnosis: Diagnosis, allowed: frozenset[str]) -> GuardrailResult:
    kept, services, rejections = [], [], []
    for hypothesis, service in zip(diagnosis.response.hypotheses, diagnosis.services):
        unknown = [evidence_id for evidence_id in hypothesis.evidence_ids if evidence_id not in allowed]
        if unknown:
            rejections.append(Rejection(rank=hypothesis.rank, service=service, unknown_ids=unknown))
            log.warning(
                "evidence guardrail dropped hypothesis %d about %s: cites %s, which the service never supplied",
                hypothesis.rank,
                service,
                unknown,
            )
            continue
        kept.append(hypothesis)
        services.append(service)
    response = AnalyzeResponse(
        hypotheses=[hypothesis.model_copy(update={"rank": rank}) for rank, hypothesis in enumerate(kept, start=1)]
    )
    return GuardrailResult(
        diagnosis=Diagnosis(response=response, services=services, adjustments=diagnosis.adjustments),
        checked=len(diagnosis.response.hypotheses),
        rejections=rejections,
    )


class GuardrailStats:
    """Counts since the process started, for GET /stats and the evaluation report. They reset on
    restart; Phase 8 persists every returned hypothesis, which allows durable counts."""

    KEYS = ("hypotheses_checked", "hypotheses_rejected", "responses_fully_rejected")

    def __init__(self):
        self._lock = threading.Lock()
        self._counts: Counter[str] = Counter()

    def record(self, result: GuardrailResult, source: Source) -> None:
        with self._lock:
            self._counts[f"{source}_hypotheses_checked"] += result.checked
            self._counts[f"{source}_hypotheses_rejected"] += len(result.rejections)
            if result.checked and not result.diagnosis.services:
                self._counts[f"{source}_responses_fully_rejected"] += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {f"{source}_{key}": self._counts[f"{source}_{key}"] for source in ("llm", "deterministic") for key in self.KEYS}
