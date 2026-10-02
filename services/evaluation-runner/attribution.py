"""
Scoring for the attribution half of the evaluation: did M3's ranker name the
service the fault was actually injected into, and did it cite real evidence?

This is the part of the plan's five metrics that had no numbers — root-cause
accuracy (top-1/top-3), MRR, and evidence validity (issue #21). The scoring
functions themselves already live in `scoring.py` and are unit-tested; what
was missing was anything that called `POST /analyze` and turned its answer
into the (ranked_services, ground_truth) pairs they expect.

Everything here is pure. The HTTP call and the queries live in `sources.py`,
so a ranking can be scored from a fixture without a running stack.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Mirrors the injector's KNOWN_SERVICES plus the datastores, which can be
# named as a root cause even though a fault is never injected into one
# directly. Used only by the cause-text fallback below.
NAMEABLE_SERVICES = (
    "catalogue-db", "carts-db", "orders-db", "user-db",
    "front-end", "catalogue", "payment", "shipping", "orders", "carts", "user",
    "rabbitmq", "edge-router", "session-db", "queue-master",
)

# How a cited evidence id says what it points at. The evidence model
# (docs/evidence-model.md) specifies `ev:` ids from the `evidence` table, but
# the pipeline currently cites the underlying source records instead. Both
# are checkable, and conflating "cited something that does not exist" with
# "cited it in the other valid format" would make the metric meaningless.
EVIDENCE_KINDS: dict[str, str] = {
    "ev:": "evidence",
    "anom-": "anomaly",
    "dep-": "deploy",
    # Past postmortems from M3's retrieval corpus. None appear in stored
    # hypotheses yet because retrieval is inert without Ollama - but the corpus
    # holds 61 rows, so the first machine with embeddings working would have
    # started citing them and every one would have scored as a dangling
    # reference. Validity would have fallen for a reason unrelated to the
    # citations being wrong.
    "incident-": "incident",
}


@dataclass(frozen=True)
class HypothesisRow:
    """One ranked hypothesis, as stored by M3's pipeline."""

    rank: int
    cause: str
    confidence: float
    evidence_ids: tuple[str, ...] = ()
    service: str | None = None


@dataclass
class AttributionResult:
    scenario_id: str
    fault_type: str
    ground_truth_service: str  # what a correct diagnosis names
    symptom_service: str  # what visibly degraded; the same, except for dependency faults
    anomaly_id: str | None
    ranked: list[str] = field(default_factory=list)
    reciprocal_rank: float = 0.0
    inferred_services: int = 0
    evidence_total: int = 0
    evidence_resolved: int = 0
    unresolved_ids: list[str] = field(default_factory=list)
    note: str | None = None

    @property
    def scored(self) -> bool:
        """Whether this scenario contributes to MRR and top-k.

        A scenario with no anomaly never reached the ranker, so it measures
        detection (already scored elsewhere), not attribution. Counting it as
        a ranking failure would charge M3 for M2's miss.
        """
        return self.anomaly_id is not None and bool(self.ranked)

    @property
    def evidence_validity(self) -> float | None:
        if not self.evidence_total:
            return None
        return self.evidence_resolved / self.evidence_total


def service_from_cause(cause: str) -> str | None:
    """Last-resort guess at which service a free-text cause blames.

    `POST /analyze` returns `cause` as prose and no service field, even though
    the pipeline stores one in the `hypotheses` table. When a ranking has to
    come from the response alone this is all there is, so it exists — but
    every use is counted in `inferred_services` and reported, because scoring
    a ranker through a regex turns a matching failure here into what looks
    like a wrong answer from the ranker.

    Longest name first, so `catalogue-db` is never read as `catalogue`.
    """
    lowered = cause.lower()
    for service in sorted(NAMEABLE_SERVICES, key=len, reverse=True):
        if service in lowered:
            return service
    return None


def ranked_services(hypotheses: list[HypothesisRow]) -> tuple[list[str], int]:
    """Order the blamed services by rank, best rank first.

    Returns the list and how many entries had to be inferred from cause text.

    Duplicates are collapsed to their best rank: two hypotheses naming the
    same service is one guess repeated, and leaving both in would let a
    ranker pad its list to improve top-3 without being any more right.
    """
    inferred = 0
    seen: set[str] = set()
    ordered: list[str] = []
    for hypothesis in sorted(hypotheses, key=lambda h: h.rank):
        service = hypothesis.service
        if not service:
            service = service_from_cause(hypothesis.cause)
            if service:
                inferred += 1
        if service and service not in seen:
            seen.add(service)
            ordered.append(service)
    return ordered, inferred


def classify_evidence_id(evidence_id: str) -> str | None:
    """Which record type a cited id refers to, or None if unrecognised."""
    for prefix, kind in EVIDENCE_KINDS.items():
        if evidence_id.startswith(prefix):
            return kind
    return None


def cited_evidence_ids(hypotheses: list[HypothesisRow]) -> list[str]:
    """Every id cited across the hypotheses, de-duplicated, order preserved.

    De-duplicated because the same anomaly is cited by most hypotheses in a
    set; counting it once per hypothesis would let one valid citation carry
    the whole score.
    """
    seen: set[str] = set()
    out: list[str] = []
    for hypothesis in sorted(hypotheses, key=lambda h: h.rank):
        for evidence_id in hypothesis.evidence_ids:
            if evidence_id not in seen:
                seen.add(evidence_id)
                out.append(evidence_id)
    return out


def score_attribution(
    scenario,
    hypotheses: list[HypothesisRow],
    anomaly_id: str | None,
    resolved_ids: set[str] | None = None,
) -> AttributionResult:
    """Score one scenario's diagnosis against its injected ground truth.

    `resolved_ids` is the subset of cited ids that were found in the database.
    Passing None means evidence was not checked, which is reported as "not
    measured" rather than as 0% valid.
    """
    from scoring import reciprocal_rank  # local: keeps this module import-light

    # The service a diagnosis should name, which differs from the one that
    # degrades for the dependency-shaped classes: pausing `catalogue-db` is
    # recorded as a `catalogue` fault, and scoring a ranking against `catalogue`
    # would mark the correct answer wrong and the symptom right. Detection still
    # scores against ground_truth_service; only attribution moves. M3's
    # scripts/eval_live.py made the same change in #44, so until this matched
    # the two harnesses reported different accuracy for the same run.
    target = getattr(scenario, "root_cause_service", scenario.ground_truth_service)
    result = AttributionResult(
        scenario_id=scenario.scenario_id,
        fault_type=scenario.fault_type,
        ground_truth_service=target,
        symptom_service=scenario.ground_truth_service,
        anomaly_id=anomaly_id,
    )
    if anomaly_id is None:
        result.note = "no anomaly in the fault window — never reached the ranker"
        return result
    if not hypotheses:
        result.note = "anomaly found but no hypotheses stored"
        return result

    result.ranked, result.inferred_services = ranked_services(hypotheses)
    result.reciprocal_rank = reciprocal_rank(result.ranked, target)

    cited = cited_evidence_ids(hypotheses)
    result.evidence_total = len(cited)
    if resolved_ids is not None:
        result.evidence_resolved = sum(1 for e in cited if e in resolved_ids)
        result.unresolved_ids = [e for e in cited if e not in resolved_ids]
    return result


def rankings_for_scoring(results: list[AttributionResult]) -> list[tuple[list[str], str]]:
    """The (ranked, truth) pairs `mean_reciprocal_rank` and `top_k_accuracy` take."""
    return [(r.ranked, r.ground_truth_service) for r in results if r.scored]


def evidence_validity(results: list[AttributionResult]) -> float | None:
    """Fraction of all cited evidence ids that resolve to a real record."""
    total = sum(r.evidence_total for r in results)
    if not total:
        return None
    return sum(r.evidence_resolved for r in results) / total
