"""
Renders evaluation results as Markdown tables and CSV.

Targets are printed next to measured values because the project plan is
explicit that they are starting hypotheses, not commitments — a
well-characterised miss is a better result than an unexplained pass, and
that argument is only available if the gap is visible.

Metrics that depend on M3's ranker are rendered as "not measured" rather
than omitted, so the report shows what the harness is ready to measure as
well as what it has measured.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from scoring import ScenarioResult, Summary

TARGETS = {
    "detection_latency": ("Median under 60 s", lambda v: v is not None and v < 60),
    "top3": ("Top-3 above 70%", lambda v: v is not None and v > 0.70),
    "mrr": ("MRR above 0.6", lambda v: v is not None and v > 0.6),
    "fp_rate": ("Under 1 per hour", lambda v: v is not None and v < 1.0),
    "evidence": ("100%", lambda v: v is not None and v >= 1.0),
}


def fmt(value, suffix="", precision=1) -> str:
    if value is None:
        return "not measured"
    if isinstance(value, float):
        return f"{value:.{precision}f}{suffix}"
    return f"{value}{suffix}"


def fmt_pct(value) -> str:
    return "not measured" if value is None else f"{value * 100:.0f}%"


def verdict(key: str, value) -> str:
    _, passes = TARGETS[key]
    if value is None:
        return "—"
    return "met" if passes(value) else "MISSED"


def render_headline(
    overall: Summary | None,
    fp_rate: float | None,
    mrr: float | None = None,
    top3: float | None = None,
    evidence_validity: float | None = None,
) -> str:
    median = overall.median_latency_s if overall else None
    rows = [
        ("Detection latency", fmt(median, " s"), TARGETS["detection_latency"][0],
         verdict("detection_latency", median)),
        ("Root-cause accuracy (Top-3)", fmt_pct(top3), TARGETS["top3"][0],
         verdict("top3", top3)),
        ("Ranking quality (MRR)", fmt(mrr, "", 2), TARGETS["mrr"][0],
         verdict("mrr", mrr)),
        ("False-positive rate", fmt(fp_rate, " /hour", 2), TARGETS["fp_rate"][0],
         verdict("fp_rate", fp_rate)),
        ("Evidence validity", fmt_pct(evidence_validity), TARGETS["evidence"][0],
         verdict("evidence", evidence_validity)),
    ]

    lines = [
        "| Metric | Measured | Target | Verdict |",
        "| --- | --- | --- | --- |",
    ]
    lines += [f"| {name} | {measured} | {target} | {status} |"
              for name, measured, target, status in rows]
    lines.append("")
    lines.append(
        "Root-cause accuracy, ranking quality and evidence validity score M3's "
        "ranker, and a `live` run does not ask it anything - the fault is already "
        "over by the time a diagnosis would be useful to score. Run "
        "`evaluation-runner attribute` against the same window for those three; "
        "they are shown here as not measured rather than as zero."
    )
    return "\n".join(lines)


def render_per_fault_type(summaries: dict[str, Summary]) -> str:
    lines = [
        "| Fault type | Scenarios | Detected | Of observable | Misattributed | Missed | Unobservable | Median latency | p95 latency |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]

    def row(name: str, s: Summary, bold: bool = False) -> str:
        mark = "**" if bold else ""
        label = f"**{name}**" if bold else f"`{name}`"
        return (
            f"| {label} | {mark}{s.total}{mark} | "
            f"{mark}{s.detected} ({fmt_pct(s.detection_rate)}){mark} | "
            f"{mark}{fmt_pct(s.detection_rate_observable)}{mark} | "
            f"{mark}{s.misattributed}{mark} | {mark}{s.missed}{mark} | "
            f"{mark}{s.unobservable}{mark} | "
            f"{mark}{fmt(s.median_latency_s, ' s')}{mark} | "
            f"{mark}{fmt(s.p95_latency_s, ' s')}{mark} |"
        )

    for name in sorted(k for k in summaries if k != "overall"):
        lines.append(row(name, summaries[name]))
    if "overall" in summaries:
        lines.append(row("overall", summaries["overall"], bold=True))

    lines.append("")
    lines.append(
        "**Detected** is over every scenario attempted — the number that cannot "
        "flatter. **Of observable** excludes scenarios where the target service "
        "emitted no request-level telemetry, so nothing could have been detected. "
        "Both are shown; quoting only the second would let a broken testbed look "
        "like a good detector."
    )
    return "\n".join(lines)


def render_scenarios(results: list[ScenarioResult]) -> str:
    lines = [
        "| Scenario | Fault type | Ground truth | Outcome | Latency | Matched event |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        lines.append(
            f"| `{r.scenario.scenario_id}` | `{r.scenario.fault_type}` | "
            f"`{r.scenario.ground_truth_service}` | {r.outcome.value} | "
            f"{fmt(r.detection_latency_s, ' s')} | "
            f"{r.matched_event_id or '—'} |"
        )
    return "\n".join(lines)


def render_ablation(by_detector: dict[str, tuple[dict[str, Summary], float | None]]) -> str:
    """One row per detector over identical replayed input."""
    lines = [
        "| Detector | Scenarios | Detected | Misattributed | Missed | Median latency | False positives/hour |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name in sorted(by_detector):
        summaries, fp_rate = by_detector[name]
        s = summaries.get("overall")
        if s is None:
            lines.append(f"| `{name}` | 0 | — | — | — | — | — |")
            continue
        lines.append(
            f"| `{name}` | {s.total} | {s.detected} ({fmt_pct(s.detection_rate)}) | "
            f"{s.misattributed} | {s.missed} | {fmt(s.median_latency_s, ' s')} | "
            f"{fmt(fp_rate, '', 2)} |"
        )
    return "\n".join(lines)


def build_report(
    title: str,
    generated_at: datetime,
    summaries: dict[str, Summary],
    results: list[ScenarioResult],
    fp_rate: float | None,
    false_positive_count: int,
    quiet_s: float,
    ablation: dict[str, tuple[dict[str, Summary], float | None]] | None = None,
    notes: list[str] | None = None,
) -> str:
    sections = [
        f"# {title}",
        "",
        f"Generated {generated_at.isoformat(timespec='seconds')} — regenerate with "
        "`docker compose run --rm evaluation-runner ...` (see docs/phase9-detection.md).",
        "",
        "## Headline metrics",
        "",
        render_headline(summaries.get("overall"), fp_rate),
        "",
        "## Detection by fault type",
        "",
        render_per_fault_type(summaries),
        "",
        "## False positives",
        "",
        f"{false_positive_count} alert(s) fired outside any fault window across "
        f"{quiet_s / 60:.1f} minutes of quiet observation "
        f"({fmt(fp_rate, ' per hour', 2)}).",
        "",
        "## Per-scenario detail",
        "",
        render_scenarios(results),
    ]

    if ablation:
        sections += [
            "",
            "## Ablation — detectors over identical replayed data",
            "",
            render_ablation(ablation),
        ]

    if notes:
        sections += ["", "## Notes", ""] + [f"- {n}" for n in notes]

    return "\n".join(sections) + "\n"


def write_csv(results: list[ScenarioResult], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "scenario_id", "fault_type", "ground_truth_service", "t_inject",
            "t_recovered", "outcome", "detection_latency_s", "matched_event_id",
            "events_in_window",
        ])
        for r in results:
            writer.writerow([
                r.scenario.scenario_id,
                r.scenario.fault_type,
                r.scenario.ground_truth_service,
                r.scenario.t_inject.isoformat(),
                r.scenario.t_recovered.isoformat() if r.scenario.t_recovered else "",
                r.outcome.value,
                "" if r.detection_latency_s is None else f"{r.detection_latency_s:.2f}",
                r.matched_event_id or "",
                r.events_in_window,
            ])


def build_attribution_report(generated_at: datetime, results: list, since: datetime) -> str:
    """Render the attribution metrics: MRR, top-k, evidence validity.

    Separate from `build_report` because it answers a different question about
    a different component — that one scores M2's detector, this one scores
    M3's ranker against the ground truth M2 injected. Merging them would hide
    which half of the pipeline a bad number came from.
    """
    from attribution import evidence_validity, rankings_for_scoring
    from scoring import mean_reciprocal_rank, top_k_accuracy

    rankings = rankings_for_scoring(results)
    mrr = mean_reciprocal_rank(rankings)
    top1 = top_k_accuracy(rankings, 1)
    top3 = top_k_accuracy(rankings, 3)
    validity = evidence_validity(results)

    scored = [r for r in results if r.scored]
    unscored = [r for r in results if not r.scored]

    lines = [
        "# Attribution evaluation",
        "",
        f"Generated {generated_at.isoformat(timespec='seconds')} · "
        f"scenarios recorded since {since.isoformat(timespec='seconds')}",
        "",
        f"**{len(scored)} of {len(results)} scenario(s) scored.** A scenario is scored only "
        "if the detector raised an anomaly for it and the ranker returned hypotheses; "
        "one that never reached the ranker measures detection, not attribution, and "
        "counting it here would charge the ranker for a detection miss.",
        "",
        "| Metric | Measured | Target | Verdict |",
        "| --- | --- | --- | --- |",
    ]
    for label, value, key, formatter in (
        ("Root-cause accuracy (Top-1)", top1, None, fmt_pct),
        ("Root-cause accuracy (Top-3)", top3, "top3", fmt_pct),
        ("Ranking quality (MRR)", mrr, "mrr", lambda v: fmt(v, "", 2)),
        ("Evidence validity", validity, "evidence", fmt_pct),
    ):
        target = TARGETS[key][0] if key else "—"
        result = verdict(key, value) if key else "—"
        lines += [f"| {label} | {formatter(value)} | {target} | {result} |"]

    lines += [
        "",
        "## Per scenario",
        "",
        "| Fault | Injected into | Ranked | RR | Evidence |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in results:
        if r.scored:
            ranked = " → ".join(
                f"**{s}**" if s == r.ground_truth_service else s for s in r.ranked[:5]
            )
            ev = (
                "—" if r.evidence_validity is None
                else f"{r.evidence_resolved}/{r.evidence_total}"
            )
            rr = f"{r.reciprocal_rank:.2f}"
        else:
            ranked, rr, ev = f"_{r.note}_", "—", "—"
        lines += [f"| `{r.fault_type}` | `{r.ground_truth_service}` | {ranked} | {rr} | {ev} |"]

    inferred = sum(r.inferred_services for r in results)
    unresolved = [e for r in results for e in r.unresolved_ids]
    notes = []
    if inferred:
        notes.append(
            f"{inferred} ranking entr(y/ies) had no `service` field and were inferred from "
            "the free-text `cause`. A parsing failure there looks identical to a wrong "
            "answer from the ranker, so these weaken the metric."
        )
    if unresolved:
        shown = ", ".join(f"`{e}`" for e in sorted(set(unresolved))[:5])
        notes.append(
            f"{len(set(unresolved))} cited evidence id(s) resolve to no record: {shown}. "
            "A hypothesis citing evidence that does not exist is the failure mode this "
            "metric exists to catch."
        )
    if unscored:
        notes.append(
            f"{len(unscored)} scenario(s) excluded: "
            + "; ".join(f"`{r.fault_type}`/{r.ground_truth_service} — {r.note}" for r in unscored)
        )
    if notes:
        lines += ["", "## Notes", ""] + [f"- {n}" for n in notes]

    return "\n".join(lines) + "\n"


def build_sweep_report(generated_at: datetime, points: list, services, detectors) -> str:
    """Render the severity sweep: detection rate against measured fault impact.

    The x-axis is measured impact, never cpu_limit. A quota of 0.002 is a no-op
    on an idle service and crippling on a busy one, so cpu_limit is not
    comparable across services or across days - which is the whole reason this
    sweep exists.
    """
    from sweep import STATIC_DETECTOR, best_adaptive_rate, crossover, verdict

    usable = [p for p in points if p.observed]
    point = crossover(usable)

    lines = [
        "# Detector ablation across fault severity",
        "",
        f"Generated {generated_at.isoformat(timespec='seconds')} · "
        f"throttling `{'`, `'.join(services)}` · detectors: {', '.join(detectors)}",
        "",
        verdict(points),
        "",
        "| cpu_limit | baseline p95 | under fault | impact | "
        + " | ".join(f"`{d}`" for d in detectors) + " |",
        "| --- | --- | --- | --- | " + " | ".join("---" for _ in detectors) + " |",
    ]
    for p in sorted(points, key=lambda p: (p.impact_ms is None, p.impact_ms or 0)):
        cells = []
        for d in detectors:
            detected, total = p.outcomes.get(d, (0, 0))
            cells.append(f"{detected}/{total}" if total else "—")
        lines.append(
            f"| {p.cpu_limit} | {fmt(p.baseline_p95_ms, ' ms')} | "
            f"{fmt(p.fault_p95_ms, ' ms')} | {fmt(p.impact_ms, ' ms')} | "
            + " | ".join(cells) + " |"
        )

    lines += ["", "## Crossover", ""]
    if point is not None:
        lines.append(
            f"At **{point.impact_ms:.0f} ms** of impact (`cpu_limit` {point.cpu_limit}) the "
            f"`{STATIC_DETECTOR}` detector reaches {fmt_pct(point.rate(STATIC_DETECTOR))}, "
            f"matching the best adaptive detector at {fmt_pct(best_adaptive_rate(point))}. "
            "Below that magnitude the learned baseline catches faults the fixed threshold "
            "does not; above it, the choice of detector stops mattering."
        )
    else:
        lines.append(
            "The fixed threshold never caught up within the range swept. The crossover is "
            "above the largest fault measured rather than absent — extend the range before "
            "claiming a learned baseline wins at every magnitude."
        )

    lines += [
        "",
        "## Reading this",
        "",
        "- The x-axis is **measured impact**, not `cpu_limit`. The same quota is a no-op on "
        "an idle service and crippling on a busy one, so only the measured number compares "
        "across services and across runs.",
        "- Each rung resets the testbed first, so a later rung is not scored against a "
        "service that has been degrading all afternoon (issue #33).",
        "- Every detector sees the identical recorded sample stream for its rung, replayed "
        "offline, so nothing separates them except the algorithm.",
    ]
    return "\n".join(lines) + "\n"
