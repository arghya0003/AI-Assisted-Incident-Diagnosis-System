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
import attribution
import sweep as sweeping
from scenarios import (
    SEVERITY_LEVELS, SUITES, partition_by_support, severity_suite,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("evaluation-runner")

# request_rate tracks ordinary traffic rather than health, and the detector
# skips it too. Replay must apply the same exclusion or it would score a
# different input than the live pipeline sees. document_count and prune_rate
# are orders-db's order history (issue #33): testbed state, skipped live too.
IGNORED_METRICS = {"request_rate", "document_count", "prune_rate"}


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
    # Ask the injector what it can actually produce before committing an hour
    # to a run. A fault type it does not implement returns 400, which the loop
    # below would log and count as a scenario the detector failed to catch.
    runnable, unsupported = partition_by_support(suite, sources.supported_fault_types())
    for spec in unsupported:
        log.warning("skipping %s: the injector does not implement %s",
                    spec.label, spec.fault_type)
    if not runnable:
        log.error("the injector implements none of the %d scenario(s) in suite '%s'",
                  len(suite), args.suite)
        return 1
    specs = [spec for spec in runnable for _ in range(args.repeat)]

    # Start from the same baseline every time: `orders` slows as the load
    # generator's order history grows, and a run on a degraded `orders`
    # scores real alerts as false positives and hands the ranker an
    # unrelated anomalous service (issue #33). Done before warm-up so the
    # detector learns the baseline the faults will actually run against.
    reset = None
    if args.no_reset:
        log.warning("--no-reset: testbed state carries over from earlier runs")
    else:
        reset = sources.reset_testbed()
        if reset is not None:
            log.info("testbed reset: removed %s order(s)", reset.get("orders_removed"))

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
    ]
    if reset is not None:
        notes.append(
            f"Testbed reset before warm-up: {reset.get('orders_removed')} accumulated "
            "order(s) cleared from orders-db, so `orders` started at its baseline latency."
        )
    else:
        notes.append(
            "Testbed was **not** reset before this run "
            + ("(`--no-reset`)" if args.no_reset else "(the load generator could not be reached)")
            + ". `orders` latency depends on how much order history had accumulated, "
              "so this run is not directly comparable with reset runs."
        )
    if unsupported:
        missing = sorted({spec.fault_type for spec in unsupported})
        notes.append(
            f"{len(unsupported)} scenario(s) skipped before injection: the deployed "
            f"fault injector does not implement "
            + ", ".join(f"`{name}`" for name in missing)
            + ". They are excluded from the denominator — a fault that never ran is "
              "not a fault the detector missed."
        )
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


def run_attribute(args) -> int:
    """Score the attribution half: did M3's ranker name the injected service?

    This closes issue #21. Detection was always scored from the detector's own
    events; these three metrics need the diagnosis layer's answer, which means
    something has to call `POST /analyze` and compare the ranking against the
    fault we injected. Nothing did.

    Deliberately a separate subcommand rather than part of `live`: a diagnosis
    can be requested for an anomaly recorded days ago, so attribution can be
    re-scored against a changed ranker without re-injecting faults. That also
    keeps a slow or broken diagnosis service from costing a run its detection
    numbers.
    """
    conn = sources.connect_postgres()
    since = now_utc() - timedelta(minutes=args.since_minutes)
    scenarios = sources.load_scenarios(conn, since=since)
    if not scenarios:
        log.error("no fault scenarios recorded since %s; run `live` first", since.isoformat())
        return 1

    def score(scenario, anomaly_id):
        """Diagnose one anomaly and score the ranking against the injected cause."""
        if anomaly_id is None:
            return attribution.score_attribution(scenario, [], None)
        hypotheses = sources.load_hypotheses(conn, anomaly_id)
        if not hypotheses and not args.no_analyze:
            log.info("asking the diagnosis service about %s", anomaly_id)
            if sources.request_diagnosis(anomaly_id, timeout=args.timeout):
                hypotheses = sources.load_hypotheses(conn, anomaly_id)
        cited = attribution.cited_evidence_ids(hypotheses)
        resolved = sources.resolve_evidence_ids(conn, cited) if cited else set()
        return attribution.score_attribution(scenario, hypotheses, anomaly_id, resolved)

    results: list[attribution.AttributionResult] = []
    unfiltered: list[attribution.AttributionResult] = []
    for scenario in scenarios:
        start, end = scoring.fault_window(scenario)
        anomaly_id = sources.find_anomaly_for_scenario(
            conn, scenario.ground_truth_service, start, end
        )
        if anomaly_id is None:
            log.info("%s (%s): no anomaly naming the injected service in the window",
                     scenario.scenario_id, scenario.ground_truth_service)
        results.append(score(scenario, anomaly_id))

        # The same window without the service filter. The headline number only
        # considers anomalies that already name the injected service, which
        # isolates ranking from grouping but silently drops the hard cases -
        # the ones where the detector surfaced a downstream symptom. Reporting
        # the gap is more honest than reporting one number as "root-cause
        # accuracy" without saying which anomalies it was allowed to see.
        any_id = sources.find_any_anomaly_in_window(conn, start, end)
        unfiltered.append(score(scenario, any_id) if any_id != anomaly_id
                          else results[-1])

    markdown = reporting.build_attribution_report(
        generated_at=now_utc(), results=results, since=since, unfiltered=unfiltered
    )
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_utc().strftime("%Y%m%dT%H%M%SZ")
    (out_dir / f"attribution-{stamp}.md").write_text(markdown, encoding="utf-8")
    (out_dir / "attribution-latest.md").write_text(markdown, encoding="utf-8")
    log.info("wrote %s", out_dir / f"attribution-{stamp}.md")
    print()
    print(markdown)
    return 0


def run_sweep(args) -> int:
    """Sweep fault severity and report where a fixed threshold catches up (#34).

    The ablation's answer depends on how hard the injected fault is, and the
    suite never controlled for that. This runs the same scenario at several
    magnitudes, replays each window through every detector, and reports
    detection rate against *measured* p95 impact rather than against cpu_limit -
    a quota that cripples one service is a no-op on another, so cpu_limit is not
    a comparable x-axis.
    """
    import replay as replaying

    conn = sources.connect_postgres()
    services = tuple(args.services)
    points: list[sweeping.SeverityPoint] = []

    for index, cpu_limit in enumerate(args.severities, start=1):
        specs = severity_suite(cpu_limit, services=services, duration_s=args.duration_seconds)
        log.info("[rung %d/%d] cpu_limit=%s on %s",
                 index, len(args.severities), cpu_limit, ", ".join(services))

        # Every rung starts from the same baseline, or the curve measures how
        # long the testbed has been up rather than fault severity (#33).
        if not args.no_reset:
            sources.reset_testbed()

        log.info("warming up detector baselines for %ss", args.warmup_seconds)
        time.sleep(args.warmup_seconds)

        rung_start = now_utc()
        scenario_ids: list[str] = []
        for spec in specs:
            try:
                scenario_ids.append(sources.inject_fault(spec))
            except Exception:
                log.exception("could not inject %s; continuing", spec.label)
                continue
            time.sleep(spec.duration_s + args.settle_seconds)
        rung_end = now_utc()

        if not scenario_ids:
            log.error("rung cpu_limit=%s injected nothing; skipping it", cpu_limit)
            continue

        scenarios = sources.load_scenarios(conn, scenario_ids=scenario_ids)
        point = sweeping.SeverityPoint(
            cpu_limit=cpu_limit,
            baseline_p95_ms=None,
            fault_p95_ms=None,
            scenario_ids=scenario_ids,
        )

        # Impact is measured on the first service only: with one service per
        # rung (the default) that is the whole picture, and with several it is
        # at least an unambiguous x-axis rather than a mean across services with
        # different baselines.
        subject = services[0]
        first = min(scenarios, key=lambda s: s.t_inject)
        point.baseline_p95_ms = sources.measure_p95(
            conn, subject, sweeping.SWEEP_METRIC,
            first.t_inject - timedelta(seconds=args.warmup_seconds), first.t_inject,
        )
        # The fault's own window, NOT through to the end of the rung. The gap
        # after a fault is 150s of recovered traffic against 90s of fault, so
        # measuring to rung_end puts the median in the healthy part: a first run
        # reported 248ms for a fault that actually reached 917ms, and ranked a
        # weaker fault above a stronger one because the dilution differed.
        point.fault_p95_ms = sources.measure_p95(
            conn, subject, sweeping.SWEEP_METRIC,
            first.t_inject, first.t_recovered or rung_end,
        )

        samples = sources.load_metric_samples(
            conn, rung_start - timedelta(seconds=args.warmup_seconds), rung_end,
            IGNORED_METRICS,
        )
        deploys = sources.load_deploys(conn, rung_start, rung_end)
        if not samples:
            log.error("no metric samples for rung cpu_limit=%s", cpu_limit)
            points.append(point)
            continue

        for kind in args.detectors:
            events = replaying.replay(samples, kind, deploys=deploys)
            results, summaries, _, _, _ = score_run(
                scenarios, events, samples[0][0], samples[-1][0]
            )
            overall = summaries["overall"]
            point.outcomes[kind] = (overall.detected, overall.total)
            log.info("  %-7s %d/%d", kind, overall.detected, overall.total)

        log.info("  impact: baseline %s ms -> %s ms",
                 point.baseline_p95_ms, point.fault_p95_ms)
        points.append(point)

    if not points:
        log.error("no rung produced a result")
        return 1

    markdown = reporting.build_sweep_report(now_utc(), points, services, args.detectors)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_utc().strftime("%Y%m%dT%H%M%SZ")
    (out_dir / f"sweep-{stamp}.md").write_text(markdown, encoding="utf-8")
    (out_dir / "sweep-latest.md").write_text(markdown, encoding="utf-8")
    log.info("wrote %s", out_dir / f"sweep-{stamp}.md")
    print()
    print(markdown)
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
    live.add_argument("--no-reset", action="store_true",
                       help="skip clearing the load generator's order history before the "
                            "run; results then depend on how long the testbed has been up")
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

    attribute = sub.add_parser(
        "attribute", help="score M3's ranking against the injected ground truth (MRR, top-k)"
    )
    attribute.add_argument("--since-minutes", type=int, default=240,
                           help="how far back to look for recorded fault scenarios")
    attribute.add_argument("--no-analyze", action="store_true",
                           help="score only already-stored hypotheses; never call /analyze")
    attribute.add_argument("--timeout", type=int, default=120,
                           help="seconds to wait for one diagnosis")
    attribute.set_defaults(func=run_attribute)

    sweep = sub.add_parser(
        "sweep", help="vary fault severity and find where a fixed threshold catches up (#34)"
    )
    sweep.add_argument("--severities", type=lambda v: [float(x) for x in v.split(",")],
                        default=list(SEVERITY_LEVELS),
                        help="cpu_limit values, weakest first")
    sweep.add_argument("--services", type=lambda v: [s.strip() for s in v.split(",") if s.strip()],
                        default=["catalogue"],
                        help="service(s) to throttle; impact is measured on the first")
    sweep.add_argument("--duration-seconds", type=int, default=90)
    sweep.add_argument("--warmup-seconds", type=int, default=90)
    sweep.add_argument("--settle-seconds", type=int, default=150,
                        help="gap after each fault; must exceed the grouper cooldown")
    sweep.add_argument("--detectors", default="ewma,static,zscore,cusum",
                        type=lambda v: [d.strip() for d in v.split(",") if d.strip()])
    sweep.add_argument("--no-reset", action="store_true",
                        help="skip the per-rung testbed reset; rungs then differ by more "
                             "than severity")
    sweep.set_defaults(func=run_sweep)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
