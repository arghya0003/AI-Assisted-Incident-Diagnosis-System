"""
Evaluation runner (M2) — the command every number in the final report comes from.

Two modes:

  live    Injects the labelled fault suite against the running testbed and
          scores what the deployed detector actually published. This is the
          honest end-to-end measurement: real faults, real pipeline, real
          detection latency.

  replay  Pulls a past window of recorded metrics back out of TimescaleDB and
          runs several detectors over that identical stream offline. This is
          where ablation numbers come from — running each detector on its own
          live window would confound the comparison with whatever else the
          testbed was doing at the time.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import report as reporting
import scoring
import sources
from scenarios import SUITES

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("evaluation-runner")

# request_rate tracks ordinary traffic rather than health, and the detector
# skips it too. Replay must apply the same exclusion or it would score a
# different input than the live pipeline sees.
IGNORED_METRICS = {"request_rate"}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def score_run(scenarios, events, observed_from, observed_until, conn=None):
    observable = {}
    for scenario in scenarios:
        if conn is None:
            observable[scenario.scenario_id] = True
            continue
        start, end = scoring.fault_window(scenario)
        observable[scenario.scenario_id] = sources.had_request_telemetry(
            conn, scenario.ground_truth_service, start, end
        )

    results = [
        scoring.score_scenario(s, events, observable=observable[s.scenario_id])
        for s in scenarios
    ]
    false_positives = scoring.find_false_positives(events, scenarios)
    quiet_s = scoring.quiet_seconds(observed_from, observed_until, scenarios)
    fp_rate = scoring.false_positives_per_hour(len(false_positives), quiet_s)
    return results, scoring.summarize(results), false_positives, quiet_s, fp_rate


def run_live(args) -> int:
    conn = sources.connect_postgres()
    collector = sources.AnomalyCollector()
    collector.start()
    log.info("listening on anomalies.detected")

    suite = SUITES[args.suite]
    specs = [spec for spec in suite for _ in range(args.repeat)]

    # The detector learns a baseline before it can flag a deviation from one.
    # Injecting during warm-up would score the detector on data it was never
    # given a chance to model.
    log.info("warming up detector baselines for %ss", args.warmup_seconds)
    time.sleep(args.warmup_seconds)

    observed_from = now_utc()
    scenario_ids: list[str] = []

    for index, spec in enumerate(specs, start=1):
        log.info("[%d/%d] injecting %s", index, len(specs), spec.label)
        try:
            scenario_id = sources.inject_fault(spec)
        except Exception:
            log.exception("could not inject %s; continuing with the rest", spec.label)
            continue

        scenario_ids.append(scenario_id)
        # Wait out the fault, then let the grouper's cooldown lapse so the
        # next scenario is not muted as a continuation of this one.
        time.sleep(spec.duration_s + args.settle_seconds)

    log.info("observing %ss of quiet time for the false-positive rate", args.quiet_seconds)
    time.sleep(args.quiet_seconds)
    observed_until = now_utc()

    collector.stop()

    if not scenario_ids:
        log.error("no faults were injected; nothing to score")
        return 1

    scenarios = sources.load_scenarios(conn, scenario_ids=scenario_ids)
    events = collector.snapshot()
    log.info("scoring %d scenarios against %d observed events", len(scenarios), len(events))

    results, summaries, false_positives, quiet_s, fp_rate = score_run(
        scenarios, events, observed_from, observed_until, conn=conn
    )

    unobservable = [r for r in results if r.outcome is scoring.Outcome.UNOBSERVABLE]
    notes = [
        f"Detector under test: `{args.detector_label}` (as deployed).",
        f"{len(events)} anomaly event(s) observed in total.",
        "Fault classes limited to the three the injector can physically produce; "
        "memory leak / OOM, dependency timeout cascade and config error are not "
        "implemented yet.",
    ]
    if unobservable:
        notes.append(
            f"{len(unobservable)} scenario(s) marked unobservable — "
            + ", ".join(f"`{r.scenario.ground_truth_service}`" for r in unobservable)
            + " emitted no request-level telemetry during the fault, so no detector "
              "could have seen it. This measures the testbed, not the detector: "
              "Sock Shop runs without a load generator, so services with no traffic "
              "report only cpu/memory. Those scenarios are still counted in the "
              "headline detection rate."
        )
    write_outputs(args, "Live evaluation run", results, summaries, fp_rate,
                   len(false_positives), quiet_s, notes=notes)
    return 0


def run_replay(args) -> int:
    import replay as replaying

    conn = sources.connect_postgres()
    since = now_utc() - timedelta(minutes=args.since_minutes)

    scenarios = sources.load_scenarios(conn, since=since)
    if not scenarios:
        log.error("no fault scenarios recorded since %s; run `live` first", since.isoformat())
        return 1

    window_start = min(s.t_inject for s in scenarios) - timedelta(minutes=args.warmup_minutes)
    window_end = max((s.t_recovered or s.t_inject) for s in scenarios) + timedelta(minutes=5)

    samples = sources.load_metric_samples(conn, window_start, window_end, IGNORED_METRICS)
    deploys = sources.load_deploys(conn, window_start, window_end)
    if not samples:
        log.error("no metric samples in %s..%s — raw metrics are retained 24h only",
                   window_start.isoformat(), window_end.isoformat())
        return 1

    log.info("replaying %d samples across %d scenarios through %s",
              len(samples), len(scenarios), args.detectors)

    observed_from = samples[0][0]
    observed_until = samples[-1][0]

    ablation: dict[str, tuple[dict, float | None]] = {}
    primary_results = primary_summaries = None
    primary_fp_rate = None
    primary_fp_count = 0
    primary_quiet = 0.0

    for kind in args.detectors:
        events = replaying.replay(samples, kind, deploys=deploys)
        results, summaries, false_positives, quiet_s, fp_rate = score_run(
            scenarios, events, observed_from, observed_until
        )
        ablation[kind] = (summaries, fp_rate)
        log.info("%-7s detected %d/%d, %d false positive(s)",
                  kind, summaries["overall"].detected, summaries["overall"].total,
                  len(false_positives))

        if primary_results is None:
            primary_results, primary_summaries = results, summaries
            primary_fp_rate, primary_fp_count, primary_quiet = (
                fp_rate, len(false_positives), quiet_s
            )

    notes = [
        f"Replayed {len(samples)} recorded samples from "
        f"{observed_from.isoformat(timespec='seconds')} to "
        f"{observed_until.isoformat(timespec='seconds')}.",
        "Every detector saw byte-identical input, so differences between rows are "
        "attributable to the detector rather than to testbed conditions.",
        f"Primary detector for the non-ablation tables: `{args.detectors[0]}`.",
    ]
    write_outputs(args, "Replay evaluation and detector ablation", primary_results,
                   primary_summaries, primary_fp_rate, primary_fp_count,
                   primary_quiet, ablation=ablation, notes=notes)
    return 0


def write_outputs(args, title, results, summaries, fp_rate, fp_count, quiet_s,
                   ablation=None, notes=None) -> None:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    markdown = reporting.build_report(
        title=title,
        generated_at=now_utc(),
        summaries=summaries,
        results=results,
        fp_rate=fp_rate,
        false_positive_count=fp_count,
        quiet_s=quiet_s,
        ablation=ablation,
        notes=notes,
    )

    stamp = now_utc().strftime("%Y%m%dT%H%M%SZ")
    md_path = out_dir / f"evaluation-{stamp}.md"
    csv_path = out_dir / f"evaluation-{stamp}.csv"
    md_path.write_text(markdown, encoding="utf-8")
    reporting.write_csv(results, csv_path)

    # A stable filename so the report and README can link to "the latest run"
    # without being edited after every execution.
    (out_dir / "latest.md").write_text(markdown, encoding="utf-8")

    log.info("wrote %s and %s", md_path, csv_path)
    print("\n" + markdown)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evaluation-runner",
        description="Inject labelled faults and score the anomaly detector.",
    )
    parser.add_argument("--out", default="/results", help="directory for reports")
    sub = parser.add_subparsers(dest="mode", required=True)

    live = sub.add_parser("live", help="inject faults and score the running detector")
    live.add_argument("--suite", choices=sorted(SUITES), default="default")
    live.add_argument("--repeat", type=int, default=1,
                       help="times to run each scenario, for a tighter median")
    live.add_argument("--warmup-seconds", type=int, default=90,
                       help="time to let detector baselines settle before injecting")
    live.add_argument("--settle-seconds", type=int, default=150,
                       help="gap after each fault; must exceed the grouper cooldown "
                            "or the next scenario is muted as a continuation")
    live.add_argument("--quiet-seconds", type=int, default=300,
                       help="fault-free observation used as the false-positive denominator")
    live.add_argument("--detector-label", default="ewma",
                       help="detector the deployed service is running, for the report")
    live.set_defaults(func=run_live)

    replay = sub.add_parser("replay", help="replay recorded metrics through several detectors")
    replay.add_argument("--since-minutes", type=int, default=120)
    replay.add_argument("--warmup-minutes", type=int, default=10,
                         help="recorded history before the first fault, to warm baselines")
    replay.add_argument("--detectors", default="ewma,static,zscore,cusum",
                         type=lambda v: [d.strip() for d in v.split(",") if d.strip()])
    replay.set_defaults(func=run_replay)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
