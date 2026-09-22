"""Deterministic root-cause candidate scoring. No LLM, no I/O.

This is the baseline the evaluation compares the LLM against, so every number here must be
explainable from its inputs. For one anomaly, the candidates are the services in the event plus
everything they call within `max_hops`. Each candidate gets four signals in 0..1:

- deploy_proximity: exp(-minutes / decay) for the candidate's most recent deploy in the
  lookback window before onset; 0 if none. Deploys after onset cannot be the cause.
- graph_proximity: 1 / (1 + hops below the nearest anomalous service in the event).
- co_anomaly: 1 if the candidate is the deepest anomalous service — anomalous itself (in this
  event, or in a related anomaly whose onset is within the window), with nothing it calls also
  anomalous. An anomalous service whose dependencies are anomalous too is more likely a symptom.
- incident_similarity: the highest cosine similarity among retrieved past incidents whose root
  cause was in this candidate; 0 if none name it. A single anomaly-wide similarity would add the
  same amount to every candidate and so could never change the ranking.

score = weighted sum of the signals, with weights from settings. Every candidate cites the
evidence behind its signals, and those evidence ids are what the LLM may later cite.
"""

import dataclasses
import math
from dataclasses import dataclass, field

from app.graph import DEFAULT_MAX_HOPS, DependencyGraph
from app.models import AnomalyEvent, Candidate, CandidateReport, Deploy, Evidence, Signals, SimilarIncident
from app.settings import Settings


@dataclass(frozen=True)
class ScoringConfig:
    weight_deploy: float
    weight_graph: float
    weight_co_anomaly: float
    weight_incident: float
    deploy_lookback_minutes: float
    deploy_decay_minutes: float
    co_anomaly_window_seconds: float
    max_hops: int = DEFAULT_MAX_HOPS

    def __post_init__(self) -> None:
        weights = self.weights
        if any(weight < 0 for weight in weights.values()):
            raise ValueError(f"score weights must be >= 0, got {weights}")
        if not math.isclose(sum(weights.values()), 1.0, abs_tol=1e-6):
            raise ValueError(f"score weights must sum to 1, got {sum(weights.values()):.4f} from {weights}")
        for name in ("deploy_lookback_minutes", "deploy_decay_minutes", "co_anomaly_window_seconds"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be > 0, got {getattr(self, name)}")
        if self.max_hops < 0:
            raise ValueError(f"max_hops must be >= 0, got {self.max_hops}")

    @property
    def weights(self) -> dict[str, float]:
        """Weight per signal, keyed by the Signals field it multiplies."""
        return {
            "deploy_proximity": self.weight_deploy,
            "graph_proximity": self.weight_graph,
            "co_anomaly": self.weight_co_anomaly,
            "incident_similarity": self.weight_incident,
        }

    @classmethod
    def from_settings(cls, settings: Settings) -> "ScoringConfig":
        return cls(
            weight_deploy=settings.score_weight_deploy,
            weight_graph=settings.score_weight_graph,
            weight_co_anomaly=settings.score_weight_co_anomaly,
            weight_incident=settings.score_weight_incident,
            deploy_lookback_minutes=settings.deploy_lookback_minutes,
            deploy_decay_minutes=settings.deploy_decay_minutes,
            co_anomaly_window_seconds=settings.co_anomaly_window_seconds,
        )

    def without_graph(self) -> "ScoringConfig":
        """The no_graph ablation: graph proximity weighted 0 and the other weights scaled back up to sum
        to 1. The graph still decides which services are candidates, and the signal is still shown."""
        remaining = 1.0 - self.weight_graph
        if remaining <= 0:
            raise ValueError("cannot remove the graph weight when it is the only non-zero weight")
        return dataclasses.replace(
            self,
            weight_deploy=self.weight_deploy / remaining,
            weight_graph=0.0,
            weight_co_anomaly=self.weight_co_anomaly / remaining,
            weight_incident=self.weight_incident / remaining,
        )


@dataclass(frozen=True)
class ScoringInputs:
    anomaly: AnomalyEvent
    # Other anomalies with a nearby onset. May include ones outside the window, or the anomaly
    # itself; scoring filters them, so callers can over-fetch.
    related: list[AnomalyEvent]
    # Deploys around the onset. Scoring applies the lookback window and per-service matching.
    deploys: list[Deploy]
    # Past incidents from retrieval (app/retrieval.py); empty when retrieval didn't run or failed.
    similar_incidents: list[SimilarIncident] = field(default_factory=list)
    retrieval_status: str = "not_run"


def score_candidates(inputs: ScoringInputs, graph: DependencyGraph, config: ScoringConfig) -> CandidateReport:
    anomaly = inputs.anomaly
    onset = anomaly.t_onset
    origins = list(dict.fromkeys(anomaly.services))

    # For each candidate: hops below the nearest service in this event, and which service that is.
    nearest: dict[str, tuple[int, str]] = {service: (0, service) for service in origins}
    for origin in origins:
        if origin not in graph:
            continue
        for service, hops in graph.downstream(origin, config.max_hops).items():
            if service not in nearest or hops < nearest[service][0]:
                nearest[service] = (hops, origin)

    related = sorted(
        (
            other
            for other in inputs.related
            if other.anomaly_id != anomaly.anomaly_id
            and abs((other.t_onset - onset).total_seconds()) <= config.co_anomaly_window_seconds
        ),
        key=lambda other: (other.t_onset, other.anomaly_id),
    )
    anomalous = set(origins).union(*(other.services for other in related))

    primary = _anomaly_evidence(anomaly, anomaly)
    evidence: dict[str, Evidence] = {primary.evidence_id: primary}

    def cite(item: Evidence) -> str:
        evidence.setdefault(item.evidence_id, item)
        return item.evidence_id

    scored = []
    for service, (hops, origin) in nearest.items():
        deploy, minutes = _latest_deploy(service, inputs.deploys, anomaly, config)
        past_incidents = [incident for incident in inputs.similar_incidents if service in incident.services]
        signals = Signals(
            deploy_proximity=math.exp(-minutes / config.deploy_decay_minutes) if deploy else 0.0,
            graph_proximity=1.0 / (1 + hops),
            co_anomaly=1.0 if _is_deepest_anomalous(service, anomalous, graph, config.max_hops) else 0.0,
            incident_similarity=max((_clip(incident.similarity) for incident in past_incidents), default=0.0),
        )
        score = sum(weight * getattr(signals, name) for name, weight in config.weights.items())

        evidence_ids = [primary.evidence_id]
        if deploy is not None:
            evidence_ids.append(cite(_deploy_evidence(anomaly, deploy, minutes, signals.deploy_proximity)))
        if hops > 0:
            evidence_ids.append(cite(_dependency_evidence(anomaly, origin, service, hops, signals.graph_proximity)))
        for other in related:
            if service in other.services:
                evidence_ids.append(cite(_anomaly_evidence(anomaly, other)))
        for incident in past_incidents:
            evidence_ids.append(cite(_incident_evidence(anomaly, incident)))

        scored.append(
            {
                "service": service,
                "kind": graph.kind(service) if service in graph else "unknown",
                "score": min(score, 1.0),  # guards float rounding just above 1
                "signals": signals,
                "distance": hops,
                "deploy_id": deploy.deploy_id if deploy else None,
                "evidence_ids": evidence_ids,
            }
        )

    # Highest score first; ties go to the service closer to the anomaly, then by name, so the
    # order is reproducible.
    scored.sort(key=lambda c: (-c["score"], c["distance"], c["service"]))
    return CandidateReport(
        anomaly_id=anomaly.anomaly_id,
        t_onset=onset,
        anomalous_services=sorted(anomalous),
        related_anomaly_ids=[other.anomaly_id for other in related],
        weights=config.weights,
        retrieval_status=inputs.retrieval_status,
        similar_incidents=list(inputs.similar_incidents),
        candidates=[Candidate(rank=rank, **c) for rank, c in enumerate(scored, start=1)],
        evidence=sorted(evidence.values(), key=lambda item: (-item.relevance, item.evidence_id)),
    )


def _latest_deploy(
    service: str, deploys: list[Deploy], anomaly: AnomalyEvent, config: ScoringConfig
) -> tuple[Deploy | None, float]:
    """The service's most recent deploy at or before onset, within the lookback window."""
    eligible = []
    for deploy in deploys:
        if deploy.service != service:
            continue
        minutes = (anomaly.t_onset - deploy.time).total_seconds() / 60
        if 0 <= minutes <= config.deploy_lookback_minutes:
            eligible.append((minutes, deploy.deploy_id, deploy))
    if not eligible:
        return None, 0.0
    minutes, _, deploy = min(eligible)
    return deploy, minutes


def _is_deepest_anomalous(service: str, anomalous: set[str], graph: DependencyGraph, max_hops: int) -> bool:
    if service not in anomalous:
        return False
    if service not in graph:
        return True
    return not any(callee in anomalous for callee in graph.downstream(service, max_hops))


def _clip(value: float) -> float:
    return max(0.0, min(1.0, value))


def _evidence_id(incident: AnomalyEvent, category: str, source_id: str) -> str:
    # Deterministic, so rescoring the same anomaly yields the same ids (and idempotent upserts).
    return f"ev:{incident.anomaly_id}:{category}:{source_id}"


def observed_values(anomaly: AnomalyEvent, limit: int = 3) -> list[str]:
    """The measured value and baseline behind the anomaly, worst signal first. Empty for an event
    published before M2 added `contributors`."""
    ranked = sorted(anomaly.contributors, key=lambda c: -(c.score if c.score is not None else 0.0))
    lines = []
    for contributor in ranked[:limit]:
        if contributor.value is None:
            continue
        baseline = "" if contributor.baseline is None else f" (baseline {contributor.baseline:.4g})"
        lines.append(f"{contributor.service} {contributor.metric} {contributor.value:.4g}{baseline}")
    return lines


def _anomaly_evidence(incident: AnomalyEvent, anomaly: AnomalyEvent) -> Evidence:
    offset = (anomaly.t_onset - incident.t_onset).total_seconds()
    what = f"{', '.join(anomaly.metrics)} anomalous on {', '.join(anomaly.services)} ({anomaly.severity})"
    observed = observed_values(anomaly)
    if observed:
        what += ": " + "; ".join(observed)
    summary = what if anomaly is incident else f"{what}, onset {offset:+.1f}s from {incident.anomaly_id}"
    return Evidence(
        evidence_id=_evidence_id(incident, "anomaly", anomaly.anomaly_id),
        incident_id=incident.anomaly_id,
        category="anomaly",
        source_id=anomaly.anomaly_id,
        service=anomaly.services[0] if len(anomaly.services) == 1 else None,
        observed_at=anomaly.t_onset,
        relevance=1.0,
        summary=summary,
        payload={
            "services": anomaly.services,
            "metrics": anomaly.metrics,
            "severity": anomaly.severity,
            "t_onset": anomaly.t_onset.isoformat(),
            "onset_offset_seconds": offset,
            "detector": anomaly.detector,
            "contributors": [c.model_dump(mode="json") for c in anomaly.contributors],
        },
    )


def _deploy_evidence(incident: AnomalyEvent, deploy: Deploy, minutes: float, relevance: float) -> Evidence:
    return Evidence(
        evidence_id=_evidence_id(incident, "deployment", deploy.deploy_id),
        incident_id=incident.anomaly_id,
        category="deployment",
        source_id=deploy.deploy_id,
        service=deploy.service,
        observed_at=deploy.time,
        relevance=relevance,
        summary=f"{deploy.service} {deploy.version} ({deploy.deploy_id}) deployed {minutes:.1f} min before onset",
        payload={
            "version": deploy.version,
            "commit_sha": deploy.commit_sha,
            "config_diff": deploy.config_diff,
            "minutes_before_onset": round(minutes, 3),
        },
    )


def _incident_evidence(incident: AnomalyEvent, past: SimilarIncident) -> Evidence:
    return Evidence(
        evidence_id=_evidence_id(incident, "similar_incident", past.incident_id),
        incident_id=incident.anomaly_id,
        category="similar_incident",
        source_id=past.incident_id,
        service=past.services[0] if len(past.services) == 1 else None,
        observed_at=incident.t_onset,
        relevance=_clip(past.similarity),
        summary=f"Similar past incident {past.incident_id}: {past.title} (similarity {past.similarity:.2f})",
        payload={
            "title": past.title,
            "services": past.services,
            "fault_type": past.fault_type,
            "source": past.source,
            "similarity": past.similarity,
        },
    )


def _dependency_evidence(
    incident: AnomalyEvent, origin: str, service: str, hops: int, relevance: float
) -> Evidence:
    return Evidence(
        evidence_id=_evidence_id(incident, "dependency", f"{origin}->{service}"),
        incident_id=incident.anomaly_id,
        category="dependency",
        source_id=f"{origin}->{service}",
        service=service,
        observed_at=incident.t_onset,
        relevance=relevance,
        summary=f"{service} is {hops} hop{'' if hops == 1 else 's'} downstream of anomalous {origin}",
        payload={"from": origin, "to": service, "hops": hops, "edge_type": "calls"},
    )
