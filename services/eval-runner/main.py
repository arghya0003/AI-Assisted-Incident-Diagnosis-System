"""
Evaluation runner (M2, Phase 2). Owns the numbers in the final report: for
each fault type in fault-injector's catalogue, inject it a few times, wait
for the anomaly detector(s) to react (or not), then report detection
latency, hit rate ("recall"), and false-positive rate per detector -
regenerable with a single command (`docker compose run --rm eval-runner`,
or `python main.py` with the env vars below pointed at a running stack).

Deliberately NOT computed here: root-cause ranking accuracy / MRR / top-k
(section 06 of the project plan). Those score M3's hypothesis ranking
against ground truth, and M3 doesn't exist in this repo yet - `evidence`
and `fault_scenarios` already carry everything M3's ranker will need
(ground_truth_service, t_inject) once it does. Scoring detection quality
(this file) doesn't need to wait on that.

Method:
  1. Quiet-period baseline: sample QUIET_PERIOD_SECONDS with no faults
     running, count anomalies per detector -> false positives/hour.
  2. For each scenario in SCENARIOS, TRIALS_PER_FAULT times: POST it to
     fault-injector, poll until it reports recovered/failed, then look for
     the first anomaly (per detector) naming the ground-truth service with
     t_detected between t_inject and t_inject + DETECTION_TIMEOUT_SECONDS.
     detection latency = t_detected - t_inject; a trial with no match by
     the timeout is a miss.
  3. Aggregate per (fault_type, detector): hit rate, median/p95 latency
     over the hits. Write both the raw per-trial rows and the aggregated
     table to results/ as CSV, and print the aggregated table.
"""

import csv
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras
import requests

FAULT_INJECTOR_URL = os.environ.get("FAULT_INJECTOR_URL", "http://fault-injector:5001")
PG_HOST = os.environ.get("PG_HOST", "timescaledb")
PG_PORT = os.environ.get("PG_PORT", "5432")
PG_DB = os.environ.get("PG_DB", "metrics")
PG_USER = os.environ.get("PG_USER", "postgres")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "Abcd1234#")

TRIALS_PER_FAULT = int(os.environ.get("TRIALS_PER_FAULT", "3"))
DETECTION_TIMEOUT_SECONDS = int(os.environ.get("DETECTION_TIMEOUT_SECONDS", "90"))
QUIET_PERIOD_SECONDS = int(os.environ.get("QUIET_PERIOD_SECONDS", "60"))
RECOVERY_POLL_SECONDS = 2
POST_RECOVERY_BUFFER_SECONDS = 5
RESULTS_DIR = os.environ.get("RESULTS_DIR", "/app/results")

DETECTORS = ["ewma", "static_threshold"]

SCENARIOS = [
    {"fault_type": "bad_deploy_latency", "service": "catalogue", "duration_s": 25, "cpu_limit": 0.05},
    {"fault_type": "service_crash", "service": "payment", "duration_s": 15},
    {"fault_type": "db_pool_saturation", "service": "catalogue", "duration_s": 20, "connections": 80},
]


def connect_postgres():
    while True:
        try:
            conn = psycopg2.connect(host=PG_HOST, port=PG_PORT, dbname=PG_DB, user=PG_USER, password=PG_PASSWORD)
            conn.autocommit = True
            return conn
        except psycopg2.OperationalError as exc:
            print(f"[eval-runner] timescaledb not reachable yet ({exc}), retrying in 3s", file=sys.stderr)
            time.sleep(3)


def parse_ts(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def count_anomalies_between(conn, detector: str, start: datetime, end: datetime) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM anomalies WHERE detector = %s AND t_detected BETWEEN %s AND %s",
            (detector, start, end),
        )
        return cur.fetchone()[0]


def first_matching_anomaly(conn, detector: str, service: str, start: datetime, end: datetime):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """SELECT anomaly_id, t_detected FROM anomalies
               WHERE detector = %s AND %s = ANY(services) AND t_detected BETWEEN %s AND %s
               ORDER BY t_detected ASC LIMIT 1""",
            (detector, service, start, end),
        )
        return cur.fetchone()


def measure_quiet_period(conn) -> dict:
    print(f"[eval-runner] quiet period: sampling {QUIET_PERIOD_SECONDS}s with no faults injected")
    start = datetime.now(timezone.utc)
    time.sleep(QUIET_PERIOD_SECONDS)
    end = datetime.now(timezone.utc)

    hours = QUIET_PERIOD_SECONDS / 3600.0
    rates = {}
    for detector in DETECTORS:
        count = count_anomalies_between(conn, detector, start, end)
        rates[detector] = count / hours if hours > 0 else float("nan")
        print(f"[eval-runner]   {detector}: {count} anomalies in {QUIET_PERIOD_SECONDS}s -> {rates[detector]:.2f}/hour")
    return rates


def trigger_fault(scenario: dict) -> dict:
    resp = requests.post(f"{FAULT_INJECTOR_URL}/faults", json=scenario, timeout=10)
    resp.raise_for_status()
    return resp.json()


def wait_for_scenario(scenario_id: str, timeout_s: int = 300) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        resp = requests.get(f"{FAULT_INJECTOR_URL}/faults", params={"limit": 50}, timeout=10)
        resp.raise_for_status()
        for row in resp.json():
            if row["scenario_id"] == scenario_id and row["status"] in ("recovered", "failed"):
                return row
        time.sleep(RECOVERY_POLL_SECONDS)
    raise TimeoutError(f"scenario {scenario_id} did not finish within {timeout_s}s")


def run_trial(conn, scenario: dict, trial_index: int) -> list[dict]:
    print(f"[eval-runner] trial {trial_index + 1}/{TRIALS_PER_FAULT}: {scenario['fault_type']} on {scenario['service']}")
    started = trigger_fault(scenario)
    scenario_id = started["scenario_id"]
    finished = wait_for_scenario(scenario_id)
    t_inject = parse_ts(finished["t_inject"])

    time.sleep(POST_RECOVERY_BUFFER_SECONDS)
    timeout_end = t_inject + timedelta(seconds=DETECTION_TIMEOUT_SECONDS)

    rows = []
    for detector in DETECTORS:
        match = first_matching_anomaly(conn, detector, scenario["service"], t_inject, timeout_end)
        detected = match is not None
        latency_s = (parse_ts(match["t_detected"]) - t_inject).total_seconds() if detected else None
        rows.append({
            "fault_type": scenario["fault_type"],
            "service": scenario["service"],
            "scenario_id": scenario_id,
            "scenario_status": finished["status"],
            "detector": detector,
            "detected": detected,
            "latency_s": latency_s,
        })
        outcome = f"detected in {latency_s:.1f}s" if detected else "MISSED"
        print(f"[eval-runner]   {detector}: {outcome}")
    return rows


def write_csv(path: str, rows: list[dict], fieldnames: list[str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def aggregate(raw_rows: list[dict], fp_rates: dict) -> list[dict]:
    summary = []
    fault_types = sorted({r["fault_type"] for r in raw_rows})
    for fault_type in fault_types:
        for detector in DETECTORS:
            trials = [r for r in raw_rows if r["fault_type"] == fault_type and r["detector"] == detector]
            hits = [r for r in trials if r["detected"]]
            latencies = [r["latency_s"] for r in hits]
            summary.append({
                "fault_type": fault_type,
                "detector": detector,
                "trials": len(trials),
                "hits": len(hits),
                "recall": round(len(hits) / len(trials), 2) if trials else None,
                "median_latency_s": round(statistics.median(latencies), 2) if latencies else None,
                "max_latency_s": round(max(latencies), 2) if latencies else None,
                "false_positives_per_hour": round(fp_rates.get(detector, float("nan")), 2),
            })
    return summary


def print_table(rows: list[dict]) -> None:
    if not rows:
        print("(no results)")
        return
    headers = list(rows[0].keys())
    widths = {h: max(len(h), *(len(str(r[h])) for r in rows)) for h in headers}
    print(" | ".join(h.ljust(widths[h]) for h in headers))
    print("-+-".join("-" * widths[h] for h in headers))
    for r in rows:
        print(" | ".join(str(r[h]).ljust(widths[h]) for h in headers))


def main():
    conn = connect_postgres()

    fp_rates = measure_quiet_period(conn)

    raw_rows = []
    for scenario in SCENARIOS:
        for trial in range(TRIALS_PER_FAULT):
            raw_rows.extend(run_trial(conn, scenario, trial))

    summary_rows = aggregate(raw_rows, fp_rates)

    write_csv(os.path.join(RESULTS_DIR, "eval_raw.csv"), raw_rows,
              ["fault_type", "service", "scenario_id", "scenario_status", "detector", "detected", "latency_s"])
    write_csv(os.path.join(RESULTS_DIR, "eval_summary.csv"), summary_rows,
              ["fault_type", "detector", "trials", "hits", "recall", "median_latency_s",
               "max_latency_s", "false_positives_per_hour"])

    print("\n[eval-runner] === summary (also written to results/eval_summary.csv) ===")
    print_table(summary_rows)


if __name__ == "__main__":
    main()
