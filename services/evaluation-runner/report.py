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
        "ranker, which is not wired up yet. The harness computes them as soon as "
        "a ranker is supplied; they are shown as not measured rather than as zero."
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
