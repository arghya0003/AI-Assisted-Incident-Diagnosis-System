"""Builds the /analyze LLM prompt and response schema from the deterministic candidate report.

The wording lives in app/prompts/*.txt rather than here, because evaluation will iterate on it.
This module decides what goes into the prompt and enforces the context budget. The response schema
binds each hypothesis to one listed candidate, with that candidate's own citable ids and actions,
and Ollama enforces it while decoding. Instructions alone were not enough: with a flat list of
allowed actions, phi4-mini proposed rolling back one service's deploy as the fix for another.
"""

import dataclasses
import logging
import math
import string
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.hypotheses import MAX_HYPOTHESES, CandidateOptions, candidate_options
from app.models import (
    LIVENESS_METRIC,
    NO_ACTION,
    AnomalyEvent,
    Candidate,
    CandidateReport,
    Deploy,
    Evidence,
    SimilarIncident,
)
from app.scoring import observed_values

log = logging.getLogger("diagnosis-service.prompts")

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
SYSTEM_PROMPT = (PROMPTS_DIR / "analyze_system.txt").read_text(encoding="utf-8").strip()
USER_TEMPLATE = string.Template((PROMPTS_DIR / "analyze_user.txt").read_text(encoding="utf-8").strip())
RETRY_TEMPLATE = string.Template((PROMPTS_DIR / "analyze_retry.txt").read_text(encoding="utf-8").strip())
# The llm_only ablation's prompt: no scores, graph positions or past incidents.
LLM_ONLY_SYSTEM_PROMPT = (PROMPTS_DIR / "analyze_llm_only_system.txt").read_text(encoding="utf-8").strip()
LLM_ONLY_USER_TEMPLATE = string.Template((PROMPTS_DIR / "analyze_llm_only_user.txt").read_text(encoding="utf-8").strip())

# Measured on phi4-mini in Phase 6: 1,983 characters of prompt became 575 prompt tokens (3.45 per
# token, chat template included), so 3.0 overestimated by about 15% and the budget erred safe.
# That margin is gone. Since the anomaly block started carrying measured values, the 2026-09-16
# ablations saw actual/estimated ratios from 0.76 to 1.40: numbers and punctuation tokenize worse
# than prose, so the estimate can now UNDER-count by about 40%. Harmless today - prompts run
# near 700 tokens against an 8192 budget - but do not treat this as a safety margin, and re-measure
# before raising the budget or shrinking num_ctx.
CHARS_PER_TOKEN = 3.0
# Schema bounds. Without them, constrained decoding was seen to repeat one allowed id until the
# output token limit (Phase 6); bounded arrays and strings make that loop impossible.
MAX_CITATIONS = 6
CAUSE_MAX_CHARS = 400
CONFIG_DIFF_LIMIT = 200


class PromptTooLarge(RuntimeError):
    """The prompt exceeds the context budget even after every allowed truncation."""


@dataclass(frozen=True)
class Prompt:
    messages: list[dict[str, str]]
    schema: dict
    options: list[CandidateOptions]  # one per candidate shown, in scorer order
    estimated_tokens: int
    truncations: list[str] = field(default_factory=list)

    @property
    def citable_ids(self) -> list[str]:
        """Every id any hypothesis may cite, the anomaly first."""
        return list(dict.fromkeys(i for option in self.options for i in option.citable_ids))

    @property
    def allowed_actions(self) -> list[str]:
        return list(dict.fromkeys(a for option in self.options for a in option.actions))


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def retry_message(errors: list[str]) -> str:
    return RETRY_TEMPLATE.substitute(errors="\n".join(f"- {error}" for error in errors))


def build_prompt(
    anomaly: AnomalyEvent,
    report: CandidateReport,
    context_tokens: int,
    response_reserve_tokens: int,
    max_candidates: int = 5,
    min_candidates: int = 3,
) -> Prompt:
    """Render the prompt, dropping detail in a fixed order until it fits the budget: deploy config
    diffs first, then candidates beyond the minimum. Every truncation is logged and recorded on the
    prompt. (Similar-incident bodies, the plan's first truncation, are never included at all.)"""
    budget = context_tokens - response_reserve_tokens
    options = {"candidates": max_candidates, "config_diffs": True}
    reductions = [
        ("dropped deploy config diffs", {"config_diffs": False}),
        (f"kept only the top {min_candidates} candidates", {"candidates": min_candidates}),
    ]
    truncations: list[str] = []
    prompt = render_prompt(anomaly, report, **options)
    for description, change in reductions:
        if prompt.estimated_tokens <= budget:
            break
        log.warning(
            "prompt for %s is ~%d tokens, over the %d-token budget: %s",
            anomaly.anomaly_id,
            prompt.estimated_tokens,
            budget,
            description,
        )
        options.update(change)
        truncations.append(description)
        prompt = render_prompt(anomaly, report, **options)
    if prompt.estimated_tokens > budget:
        raise PromptTooLarge(
            f"prompt for {anomaly.anomaly_id} is ~{prompt.estimated_tokens} tokens after every truncation; "
            f"budget is {budget}"
        )
    return Prompt(
        messages=prompt.messages,
        schema=prompt.schema,
        options=prompt.options,
        estimated_tokens=prompt.estimated_tokens,
        truncations=truncations,
    )


def render_prompt(anomaly: AnomalyEvent, report: CandidateReport, candidates: int, config_diffs: bool) -> Prompt:
    evidence = {item.evidence_id: item for item in report.evidence}
    shown = report.candidates[:candidates]
    options = [candidate_options(report, candidate) for candidate in shown]
    related = [
        item for item in report.evidence if item.category == "anomaly" and item.source_id != anomaly.anomaly_id
    ]
    user = USER_TEMPLATE.substitute(
        anomaly=_anomaly_block(anomaly, related),
        candidates="\n".join(
            _candidate_block(rank, candidate, option, evidence, report.anomaly_id, config_diffs)
            for rank, (candidate, option) in enumerate(zip(shown, options), start=1)
        ),
        incidents=_incident_block(report.similar_incidents),
    )
    return Prompt(
        messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}],
        schema=response_schema(options),
        options=options,
        estimated_tokens=estimate_tokens(SYSTEM_PROMPT + user),
    )


def response_schema(options: list[CandidateOptions]) -> dict:
    """One variant per candidate: the service is fixed, and evidence ids and actions are limited to
    that candidate's own. Validation and the evidence guardrail still check the result."""
    variants = [
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["rank", "service", "cause", "confidence", "evidence_ids", "proposed_action"],
            "properties": {
                "rank": {"type": "integer", "minimum": 1, "maximum": MAX_HYPOTHESES},
                "service": {"type": "string", "enum": [option.service]},
                "cause": {"type": "string", "minLength": 1, "maxLength": CAUSE_MAX_CHARS},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "evidence_ids": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": min(MAX_CITATIONS, len(option.citable_ids)),
                    "items": {"type": "string", "enum": option.citable_ids},
                },
                "proposed_action": {"type": "string", "enum": option.actions},
            },
        }
        for option in options
    ]
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["hypotheses"],
        "properties": {
            "hypotheses": {"type": "array", "minItems": 1, "maxItems": MAX_HYPOTHESES, "items": {"anyOf": variants}}
        },
    }


def build_llm_only_prompt(
    anomaly: AnomalyEvent,
    related: list[AnomalyEvent],
    deploys: list[Deploy],
    services: list[str],
    window_seconds: float,
    lookback_minutes: float,
    rollback_max_minutes: float,
    context_tokens: int,
    response_reserve_tokens: int,
) -> Prompt:
    """The llm_only ablation's prompt: the anomaly, related anomalies and raw deploys, with every
    service as a possible cause and no scores, graph or retrieval. Each service may cite the anomaly,
    related anomalies naming it and its own deploys, and may propose rolling back its own deploys
    within `rollback_max_minutes`: the same limits the scored modes apply."""
    budget = context_tokens - response_reserve_tokens
    prompt = _render_llm_only(anomaly, related, deploys, services, window_seconds, lookback_minutes, rollback_max_minutes, True)
    truncations = []
    if prompt.estimated_tokens > budget:
        log.warning("llm_only prompt for %s is ~%d tokens, over the %d-token budget: dropped deploy config diffs",
                    anomaly.anomaly_id, prompt.estimated_tokens, budget)
        truncations.append("dropped deploy config diffs")
        prompt = _render_llm_only(anomaly, related, deploys, services, window_seconds, lookback_minutes, rollback_max_minutes, False)
    if prompt.estimated_tokens > budget:
        raise PromptTooLarge(
            f"llm_only prompt for {anomaly.anomaly_id} is ~{prompt.estimated_tokens} tokens without config diffs; "
            f"budget is {budget}"
        )
    return dataclasses.replace(prompt, truncations=truncations)


def _render_llm_only(
    anomaly: AnomalyEvent,
    related: list[AnomalyEvent],
    deploys: list[Deploy],
    services: list[str],
    window_seconds: float,
    lookback_minutes: float,
    rollback_max_minutes: float,
    config_diffs: bool,
) -> Prompt:
    onset = anomaly.t_onset
    nearby = sorted(
        (
            other
            for other in related
            if other.anomaly_id != anomaly.anomaly_id and abs((other.t_onset - onset).total_seconds()) <= window_seconds
        ),
        key=lambda other: (other.t_onset, other.anomaly_id),
    )
    recent = sorted(
        (
            ((onset - deploy.time).total_seconds() / 60, deploy)
            for deploy in deploys
            if 0 <= (onset - deploy.time).total_seconds() / 60 <= lookback_minutes
        ),
        key=lambda pair: (pair[0], pair[1].deploy_id),
    )

    options = []
    for service in services:
        own = [(minutes, deploy) for minutes, deploy in recent if deploy.service == service]
        citable = [anomaly.anomaly_id]
        citable += [other.anomaly_id for other in nearby if service in other.services]
        citable += [deploy.deploy_id for _, deploy in own]
        actions = [NO_ACTION] + [f"rollback_deploy:{d.deploy_id}" for minutes, d in own if minutes <= rollback_max_minutes]
        options.append(
            CandidateOptions(
                service=service,
                citable_ids=list(dict.fromkeys(citable)),
                actions=actions,
                has_recent_deploy=bool(own),
            )
        )

    lines = _anomaly_lines(anomaly)
    if nearby:
        lines.append("related anomalies with a nearby onset:")
        for other in nearby:
            offset = (other.t_onset - onset).total_seconds()
            lines.append(
                f"  {other.anomaly_id}: {', '.join(other.metrics)} on {', '.join(other.services)} "
                f"({other.severity}), onset {offset:+.1f} s"
            )
    else:
        lines.append("related anomalies with a nearby onset: none")

    deploy_lines = []
    for minutes, deploy in recent:
        line = f"{deploy.deploy_id}: {deploy.service} version {deploy.version}, {minutes:.1f} min before onset"
        if config_diffs and deploy.config_diff:
            line += f"; config diff: {_truncate(deploy.config_diff, CONFIG_DIFF_LIMIT)}"
        deploy_lines.append(line)

    user = LLM_ONLY_USER_TEMPLATE.substitute(
        anomaly="\n".join(lines),
        lookback=f"{lookback_minutes:g}",
        deploys="\n".join(deploy_lines) or "none",
        services="\n".join(
            f"{option.service}\n   may cite: {', '.join(option.citable_ids)}\n   may propose: {', '.join(option.actions)}"
            for option in options
        ),
    )
    return Prompt(
        messages=[{"role": "system", "content": LLM_ONLY_SYSTEM_PROMPT}, {"role": "user", "content": user}],
        schema=response_schema(options),
        options=options,
        estimated_tokens=estimate_tokens(LLM_ONLY_SYSTEM_PROMPT + user),
    )


def _anomaly_lines(anomaly: AnomalyEvent) -> list[str]:
    """What the detector actually measured. Everything after the onset line is optional: an event
    recorded before M2 added these fields still renders, just with less detail."""
    lines = [
        f"anomaly_id: {anomaly.anomaly_id}",
        f"services: {', '.join(anomaly.services)}",
        f"metrics: {', '.join(anomaly.metrics)}",
        f"severity: {anomaly.severity}",
        f"onset: {_iso(anomaly.t_onset)}",
        f"evidence window: {_iso(anomaly.evidence_window.start)} to {_iso(anomaly.evidence_window.end)}",
    ]
    if anomaly.detector:
        lines.append(f"detector: {anomaly.detector}")
    if LIVENESS_METRIC in anomaly.metrics:
        lines.append(
            f"note: a {LIVENESS_METRIC} anomaly means the service stopped reporting metrics altogether, "
            "which is what a crashed or unreachable service looks like"
        )
    observed = observed_values(anomaly)
    lines.append(
        "observed and baseline values: " + "; ".join(observed)
        if observed
        else "observed and baseline values: not provided by the anomaly detector"
    )
    if anomaly.related_deploy_ids:
        lines.append(f"deploys the detector linked to this anomaly: {', '.join(anomaly.related_deploy_ids)}")
    elif anomaly.in_deploy_window:
        lines.append("a service was mid-deploy when this anomaly started")
    return lines


def _anomaly_block(anomaly: AnomalyEvent, related: list[Evidence]) -> str:
    lines = _anomaly_lines(anomaly)
    if not related:
        lines.append("related anomalies with a nearby onset: none")
    else:
        lines.append("related anomalies with a nearby onset:")
        for item in related:
            p = item.payload
            lines.append(
                f"  {item.source_id}: {', '.join(p['metrics'])} on {', '.join(p['services'])} "
                f"({p['severity']}), onset {p['onset_offset_seconds']:+.1f} s"
            )
    return "\n".join(lines)


def _candidate_block(
    rank: int,
    candidate: Candidate,
    option: CandidateOptions,
    evidence: dict[str, Evidence],
    anomaly_id: str,
    config_diffs: bool,
) -> str:
    items = [evidence[evidence_id] for evidence_id in candidate.evidence_ids]
    lines = [f"{rank}. {candidate.service} ({candidate.kind}), score {candidate.score:.2f}"]

    deploys = [item for item in items if item.category == "deployment"]
    if deploys:
        deploy = deploys[0]
        lines.append(
            f"   recent deploy: {deploy.source_id}, version {deploy.payload['version']}, "
            f"{deploy.payload['minutes_before_onset']:.1f} min before onset "
            f"(deploy score {candidate.signals.deploy_proximity:.2f})"
        )
        if config_diffs and deploy.payload.get("config_diff"):
            lines.append(f"   config diff: {_truncate(deploy.payload['config_diff'], CONFIG_DIFF_LIMIT)}")
    else:
        lines.append("   recent deploy: none in the lookback window")

    dependencies = [item for item in items if item.category == "dependency"]
    lines.append(f"   position: {dependencies[0].summary}" if dependencies else "   position: one of the anomalous services")
    deepest = "yes" if candidate.signals.co_anomaly else "no"
    lines.append(f"   deepest anomalous service (anomalous, and nothing it calls is): {deepest}")
    related = [item.source_id for item in items if item.category == "anomaly" and item.source_id != anomaly_id]
    lines.append(f"   related anomalies on it: {', '.join(related) or 'none'}")
    incidents = [
        f"{item.source_id} (similarity {item.payload['similarity']:.2f})"
        for item in items
        if item.category == "similar_incident"
    ]
    lines.append(f"   similar past incidents with the root cause here: {', '.join(incidents) or 'none'}")
    lines.append(f"   may cite: {', '.join(option.citable_ids)}")
    lines.append(f"   may propose: {', '.join(option.actions)}")
    return "\n".join(lines)


def _incident_block(incidents: list[SimilarIncident]) -> str:
    """Id, title, fault type, root-cause service and similarity only. The root-cause and resolution
    text is deliberately left out: phi4-mini copied it into causes as if it described the current
    anomaly (PLAN.md, Phase 6 outcome)."""
    if not incidents:
        return "none retrieved"
    lines = []
    for incident in incidents:
        root = ", ".join(incident.services) or "not a Sock Shop service"
        lines.append(
            f"{incident.incident_id}: {incident.title} (root cause in: {root}; "
            f"fault type: {incident.fault_type or 'unknown'}; similarity {incident.similarity:.2f})"
        )
    return "\n".join(lines)


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
