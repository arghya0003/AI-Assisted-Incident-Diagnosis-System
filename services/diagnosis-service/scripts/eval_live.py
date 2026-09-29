"""Evaluate the ranker against real injected faults, not fixtures (issues #21 and #22).

Every accuracy number recorded so far came from `fixtures/anomalies/*.json`: events written by the
same person who wrote the corpus, with tidy contributor lists and a single obvious deploy. Real
`anomalies.detected` events are messier - background deploys from `deploy-emitter` compete with the
injected one, neighbouring services go anomalous in the same window, and sometimes nothing is
detected at all. Those are exactly the conditions that separate a ranker that works from one that
looks like it works.

What one scenario does:

  1. `POST fault-injector /faults` with a known service and fault type. The injector records
     `ground_truth_service`, which is the label - nothing here infers it.
  2. Wait for M2's detector to publish an anomaly. Attribution is deliberately conservative: the
     first anomaly whose onset falls inside the fault window. A scenario where nothing is detected
     is reported as `undetected`, never dropped - an undetected fault is a real result about the
     system, and silently excluding it would flatter every accuracy number below.
  3. `POST /analyze` for that anomaly in each requested mode.
  4. Score: is rank 1 the ground truth (top-1), is it in the top 3, reciprocal rank for MRR, and
     does every cited evidence id resolve to a record that exists (evidence validity)?

The metrics are the ones the plan asks for and `services/evaluation-runner/report.py` still renders
as "not measured". They are computed the same way M2's `scoring.py` computes them, over
(ranked_services, ground_truth) pairs, so the two agree by construction.

Budget note: each scenario costs one LLM call per non-deterministic mode, and OpenRouter's free
tier allows 50 requests a day. The defaults below are sized for that.

Run from the host with the stack up, `load-generator` running, and a key in .env:
    python services/diagnosis-service/scripts/eval_live.py --modes full,deterministic
    python services/diagnosis-service/scripts/eval_live.py --dry-run     # plan only, no faults
"""

import argparse
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("PG_HOST", "localhost")

from app.db import connect  # noqa: E402
from app.settings import Settings  # noqa: E402

# Fault types the injector can produce, against services worth separating: a leaf with no
# downstream (payment), one with a caller that degrades with it (catalogue), and a database-backed
# one (catalogue again, saturated). Kept short because the free tier allows 50 requests a day.
DEFAULT_SCENARIOS = [
    ("service_crash", "payment"),
    ("service_crash", "catalogue"),
    ("bad_deploy_latency", "catalogue"),
    ("bad_deploy_latency", "payment"),
    ("db_pool_saturation", "catalogue"),
]
UNDETECTED = "undetected"


@dataclass
class Scenario:
    fault_type: str
    service: str  # the ground truth: what was actually broken
    scenario_id: str = ""
    t_inject: datetime | None = None
    anomaly_id: str = ""
    detected_after_seconds: float | None = None
    onset_after_seconds: float | None = None
    anomaly_services: list[str] = field(default_factory=list)
    anomaly_metrics: list[str] = field(default_factory=list)
    runs: list[dict] = field(default_factory=list)  # one per mode

    @property
    def detected(self) -> bool:
        return bool(self.anomaly_id)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def reciprocal_rank(ranked_services: list[str], truth: str) -> float:
    """1/position of the true cause, 0 if absent. The same definition as M2's scoring.py, so the
    two harnesses report the same number for the same ranking."""
    for position, service in enumerate(ranked_services, start=1):
        if service == truth:
            return 1.0 / position
    return 0.0


def inject(client: httpx.Client, injector_url: str, scenario: Scenario, duration_s: int) -> None:
    body = {"service": scenario.service, "fault_type": scenario.fault_type, "duration_s": duration_s}
    response = client.post(f"{injector_url}/faults", json=body, timeout=30.0)
    response.raise_for_status()
    payload = response.json()
    scenario.scenario_id = payload["scenario_id"]
    scenario.t_inject = datetime.fromisoformat(payload["t_inject"].replace("Z", "+00:00"))
    # The injector reports the ground truth back; assert rather than assume, so a mismatch is a
    # loud failure instead of a silently mislabelled result.
    assert payload["ground_truth_service"] == scenario.service, payload


def wait_for_anomaly(settings: Settings, scenario: Scenario, timeout_s: float, duration_s: int,
                     grace_s: float, poll_s: float = 5.0) -> None:
    """The first anomaly whose onset is inside the fault window, by onset then id.

    Onset rather than detection time: a detector that fires late still describes something that
    began during the fault. The window is bounded at both ends on purpose. Anything starting before
    the injection belongs to an earlier scenario or to background noise; anything starting after the
    fault has been withdrawn is something else again, and this detector is known to emit unrelated
    noise (issue #2 was a memory-metric flood). Attributing either would credit or blame this
    ranking for an event it never saw.

    The window extends `grace_s` past the end of the fault. That is not slack for its own sake: a
    detector aggregates over a trailing window, so an anomaly caused by the fault can have an onset
    a little after the fault was withdrawn. The first version of this script ended the window at the
    fault duration exactly and recorded a payment crash as undetected, when the detector had in fact
    fired with an onset 3.9 s later - which would have reported M2's detector as having missed a
    fault it caught.

    The anomaly it picks is printed with the services and metrics it named, and with how far its
    onset fell after the injection, so a reader can judge the attribution rather than take it on
    trust.
    """
    deadline = time.monotonic() + timeout_s
    window_start = scenario.t_inject
    window_end = scenario.t_inject + timedelta(seconds=duration_s + grace_s)
    while time.monotonic() < deadline:
        conn = connect(settings)
        try:
            with conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT anomaly_id, services, metrics, t_onset, t_detected
                    FROM anomalies
                    WHERE source <> 'fixture' AND t_onset >= %s AND t_onset <= %s
                    ORDER BY t_onset, anomaly_id
                    LIMIT 1
                    """,
                    (window_start, window_end),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        if row is not None:
            anomaly_id, services, metrics, t_onset, t_detected = row
            scenario.anomaly_id = anomaly_id
            scenario.anomaly_services = list(services or [])
            scenario.anomaly_metrics = list(metrics or [])
            scenario.detected_after_seconds = (t_detected - scenario.t_inject).total_seconds()
            scenario.onset_after_seconds = (t_onset - scenario.t_inject).total_seconds()
            return
        time.sleep(poll_s)


def resolvable(settings: Settings, evidence_ids: list[str]) -> tuple[int, list[str]]:
    """How many cited ids resolve to a record that exists, and which do not.

    Evidence validity in the plan means "every citation points at something real". The guardrail
    already refuses ids that were never supplied to the model; this is the stronger check that what
    was supplied exists in the database M4's approver will look it up in.
    """
    if not evidence_ids:
        return 0, []
    conn = connect(settings)
    unresolved = []
    try:
        with conn, conn.cursor() as cur:
            for evidence_id in evidence_ids:
                cur.execute(
                    """
                    SELECT EXISTS (SELECT 1 FROM anomalies WHERE anomaly_id = %(id)s)
                        OR EXISTS (SELECT 1 FROM deploys WHERE deploy_id = %(id)s)
                        OR EXISTS (SELECT 1 FROM incidents WHERE incident_id = %(id)s)
                        OR EXISTS (SELECT 1 FROM evidence WHERE evidence_id = %(id)s)
                    """,
                    {"id": evidence_id},
                )
                if not cur.fetchone()[0]:
                    unresolved.append(evidence_id)
    finally:
        conn.close()
    return len(evidence_ids) - len(unresolved), unresolved


def analyze(client: httpx.Client, service_url: str, anomaly_id: str, mode: str, timeout_s: float) -> dict:
    started = time.monotonic()
    response = client.post(
        f"{service_url}/analyze",
        params={"mode": mode, "refresh": "true"},  # refresh: measure the ranker, not the cache
        json={"anomaly_id": anomaly_id},
        timeout=timeout_s,
    )
    latency_ms = int((time.monotonic() - started) * 1000)
    response.raise_for_status()
    body = response.json()
    return {
        "mode": mode,
        "answered_by": response.headers.get("X-Diagnosis-Mode", "unknown"),
        "model": response.headers.get("X-Model-Version", "unknown"),
        "attempts": int(response.headers.get("X-LLM-Attempts", 0)),
        "guardrail_rejected": int(response.headers.get("X-Guardrail-Rejected", 0)),
        "analysis_id": response.headers.get("X-Analysis-Id", ""),
        "latency_ms": latency_ms,
        "hypotheses": body.get("hypotheses", []),
    }


def ranked_services(settings: Settings, analysis_id: str, anomaly_id: str) -> list[str]:
    """The candidate each hypothesis is about, in rank order. The /analyze contract deliberately
    has no service field - M4 gets a cause and an action - so the stored run is where evaluation
    reads the ranking from. That column exists for exactly this."""
    conn = connect(settings)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT service FROM hypotheses WHERE analysis_id = %s AND service IS NOT NULL ORDER BY rank",
                (analysis_id,),
            )
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def run_scenario(clients, settings: Settings, scenario: Scenario, args) -> None:
    print(f"\n=== {scenario.fault_type} on {scenario.service} ===")
    inject(clients["http"], args.injector_url, scenario, args.duration)
    print(f"  injected {scenario.scenario_id} at {iso(scenario.t_inject)}")

    wait_for_anomaly(settings, scenario, args.detect_timeout, args.duration, args.grace)
    if not scenario.detected:
        print(f"  NOT DETECTED within {args.detect_timeout:.0f}s - recorded as undetected, not skipped")
        return
    print(
        f"  detected {scenario.anomaly_id} after {scenario.detected_after_seconds:.1f}s "
        f"(onset +{scenario.onset_after_seconds:.1f}s; {', '.join(scenario.anomaly_metrics)} "
        f"on {', '.join(scenario.anomaly_services)})"
    )
    if scenario.service not in scenario.anomaly_services:
        # Worth seeing: the detector named only the symptoms, so the ranker has to reach the cause
        # from services that are not in the event at all.
        print(f"     note: {scenario.service} is not among the anomalous services")

    for mode in args.modes:
        run = analyze(clients["http"], args.service_url, scenario.anomaly_id, mode, args.analyze_timeout)
        run["ranked"] = ranked_services(settings, run["analysis_id"], scenario.anomaly_id)
        cited = [i for h in run["hypotheses"] for i in h.get("evidence_ids", [])]
        run["cited"], run["unresolved"] = len(cited), resolvable(settings, cited)[1]
        run["truth"] = scenario.service
        run["rr"] = reciprocal_rank(run["ranked"], scenario.service)
        scenario.runs.append(run)
        top = run["ranked"][0] if run["ranked"] else "(none)"
        hit = "OK " if top == scenario.service else "   "
        print(
            f"  {hit}{mode:<14} rank1={top:<12} rr={run['rr']:.2f} {run['answered_by']:<22} "
            f"{run['latency_ms']:6d}ms cited={run['cited']} unresolved={len(run['unresolved'])}"
        )

    # Let the service recover and the detector settle before the next injection, so one scenario's
    # anomalies cannot be attributed to the next.
    time.sleep(args.settle)


def report(scenarios: list[Scenario], modes: list[str]) -> None:
    detected = [s for s in scenarios if s.detected]
    print("\n" + "=" * 96)
    print(f"Scenarios: {len(scenarios)} injected, {len(detected)} detected, "
          f"{len(scenarios) - len(detected)} undetected")
    if detected:
        delays = [s.detected_after_seconds for s in detected]
        print(f"Detection delay: median {statistics.median(delays):.1f}s, max {max(delays):.1f}s")

    print("\nPer mode, over the scenarios that were detected")
    print(f"{'mode':<14} {'top-1':>8} {'top-3':>8} {'MRR':>6} {'evidence':>9} {'answered by llm':>16} {'p50 ms':>8}")
    for mode in modes:
        runs = [r for s in detected for r in s.runs if r["mode"] == mode]
        if not runs:
            continue
        top1 = sum(1 for r in runs if r["ranked"] and r["ranked"][0] == r["truth"])
        top3 = sum(1 for r in runs if r["truth"] in r["ranked"][:3])
        mrr = statistics.fmean(r["rr"] for r in runs)
        cited = sum(r["cited"] for r in runs)
        unresolved = sum(len(r["unresolved"]) for r in runs)
        validity = "n/a" if not cited else f"{(cited - unresolved) / cited * 100:.0f}%"
        llm = sum(1 for r in runs if r["answered_by"] == "llm")
        p50 = statistics.median(r["latency_ms"] for r in runs)
        print(f"{mode:<14} {top1}/{len(runs):<6} {top3}/{len(runs):<6} {mrr:>6.2f} {validity:>9} "
              f"{llm}/{len(runs):<14} {p50:>8.0f}")

    undetected = [s for s in scenarios if not s.detected]
    if undetected:
        print("\nUndetected (a detection gap, not a ranking failure - reported, not hidden):")
        for s in undetected:
            print(f"  {s.fault_type} on {s.service} ({s.scenario_id})")

    unresolved_any = {i for s in scenarios for r in s.runs for i in r["unresolved"]}
    if unresolved_any:
        print(f"\nEvidence ids that did not resolve: {', '.join(sorted(unresolved_any))}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--modes", default="full,deterministic", help="comma-separated pipeline modes")
    parser.add_argument("--scenarios", default="", help="'fault_type:service,...' (default: a spread of five)")
    parser.add_argument("--duration", type=int, default=90, help="fault duration in seconds")
    parser.add_argument("--detect-timeout", type=float, default=150.0, help="how long to wait for an anomaly")
    parser.add_argument("--analyze-timeout", type=float, default=180.0)
    parser.add_argument("--settle", type=float, default=45.0, help="pause between scenarios so they do not overlap")
    parser.add_argument("--grace", type=float, default=90.0,
                        help="how long after the fault ends an anomaly may still be attributed to it")
    parser.add_argument("--service-url", default=os.environ.get("DIAGNOSIS_URL", "http://localhost:8000"))
    parser.add_argument("--injector-url", default=os.environ.get("INJECTOR_URL", "http://localhost:5001"))
    parser.add_argument("--dry-run", action="store_true", help="print the plan and the request budget, inject nothing")
    args = parser.parse_args()

    args.modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    pairs = DEFAULT_SCENARIOS
    if args.scenarios:
        pairs = [tuple(part.split(":", 1)) for part in args.scenarios.split(",") if part.strip()]
    scenarios = [Scenario(fault_type=f, service=s) for f, s in pairs]

    llm_modes = [m for m in args.modes if m != "deterministic"]
    budget = len(scenarios) * len(llm_modes)
    minutes = len(scenarios) * (args.duration + args.settle) / 60
    print(f"{len(scenarios)} scenarios x {len(args.modes)} modes -> up to {budget} LLM requests, "
          f"about {minutes:.0f} min (free tier allows 50/day)")
    for scenario in scenarios:
        print(f"  {scenario.fault_type} on {scenario.service}")
    if args.dry_run:
        return 0

    settings = Settings.from_env()
    with httpx.Client() as http:
        health = http.get(f"{args.service_url}/health", timeout=10.0).json()
        print(f"diagnosis-service {health['version']} corpus={health.get('corpus_incidents')} "
              f"retrieval={health.get('retrieval')}")
        if not health.get("corpus_incidents"):
            print("  WARNING: the corpus is empty, so incident similarity contributes nothing")
        for scenario in scenarios:
            run_scenario({"http": http}, settings, scenario, args)

    report(scenarios, args.modes)
    Path("results").mkdir(exist_ok=True)
    out = Path("results") / f"eval_live_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.json"
    out.write_text(json.dumps([vars(s) for s in scenarios], indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
