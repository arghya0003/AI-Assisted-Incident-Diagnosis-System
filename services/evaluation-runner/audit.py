"""Audit every recorded sample for false positives, window widths and grouping.

Answers GitHub issues #2 (memory_bytes false-alarm flood), #3 (zero-width
evidence_window) and #4 (no grouping, anomaly_id collisions) from real data
rather than from a one-off demo: it replays everything still in TimescaleDB
through each detector and reports

  - alerts outside fault windows, per detector, with memory_bytes counted
    separately, as false positives per fault-free hour
  - how far memory_bytes actually moves between samples on each service
  - evidence_window widths
  - how many events group several metrics or services, and duplicate ids

Time the stack was down (no samples for over a minute) is left out of the
fault-free denominator, so a laptop asleep overnight cannot flatter the rate.

    docker compose run --rm --entrypoint python evaluation-runner audit.py
"""
import statistics
import sys
from collections import Counter, defaultdict
from datetime import timedelta

sys.path.insert(0, "/app")

import sources  # noqa: E402
import replay  # noqa: E402  (puts the anomaly-detector modules on sys.path)
from deploy_window import DeployWindowTracker  # noqa: E402
from detectors import build_detector  # noqa: E402
from grouping import AnomalyGrouper  # noqa: E402
from scoring import parse_ts  # noqa: E402
from staleness import StalenessMonitor  # noqa: E402

GRACE = timedelta(seconds=90)      # same post-recovery grace the scorer uses
GAP = timedelta(seconds=60)        # no samples for this long = the stack was down
AFTER_GAP = timedelta(seconds=120)  # events this soon after a gap are about the gap


def merge(intervals):
    out = []
    for a, b in sorted(intervals):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def overlap_seconds(spans, windows):
    total = 0.0
    for a, b in spans:
        for c, d in windows:
            lo, hi = max(a, c), min(b, d)
            if hi > lo:
                total += (hi - lo).total_seconds()
    return total


conn = sources.connect_postgres()
with conn.cursor() as cur:
    cur.execute("SELECT min(time), max(time) FROM metrics")
    start, end = cur.fetchone()
    cur.execute(
        "SELECT t_inject, coalesce(t_recovered, t_inject) FROM fault_scenarios "
        "WHERE t_inject BETWEEN %s AND %s", (start, end))
    fault_windows = merge([[parse_ts(a), parse_ts(b) + GRACE] for a, b in cur.fetchall()])

samples = sources.load_metric_samples(conn, start, end, {"request_rate"})
deploys = sources.load_deploys(conn, start, end)

# Periods the stack was actually up, from the samples themselves.
times = sorted({s[0] for s in samples})
spans, gaps = [], []
span_start = times[0]
for prev, cur_t in zip(times, times[1:]):
    if cur_t - prev > GAP:
        spans.append([span_start, prev])
        gaps.append((prev, cur_t))
        span_start = cur_t
spans.append([span_start, times[-1]])

covered_s = sum((b - a).total_seconds() for a, b in spans)
quiet_s = covered_s - overlap_seconds(spans, fault_windows)

print(f"DATA  {start:%d %b %H:%M} -> {end:%d %b %H:%M} UTC, {len(samples)} samples, "
      f"{len(fault_windows)} fault windows")
print(f"      stack up {covered_s/3600:.2f} h, of which fault-free {quiet_s/3600:.2f} h")
for a, b in gaps:
    print(f"      gap {a:%d %b %H:%M:%S} -> {b:%d %b %H:%M:%S} ({(b - a).total_seconds()/60:.0f} min, no data)")


def classify(t):
    if any(a <= t <= b for a, b in fault_windows):
        return "fault"
    if any(b <= t <= b + AFTER_GAP for _, b in gaps):
        return "gap"
    return "quiet"


def run(kind):
    tracker = DeployWindowTracker(window_seconds=120)
    for d in deploys:
        tracker.record(*d)
    grouper = AnomalyGrouper(group_delay_seconds=15, cooldown_seconds=120)
    stale = StalenessMonitor(stale_after_seconds=30)
    detectors, signals, events = {}, [], []
    for t, service, metric, value in samples:
        stale.observe(service, t)
        key = (service, metric)
        if key not in detectors:
            detectors[key] = build_detector(kind, service=service, metric=metric,
                                            warmup=10, required_breaches=2)
        needed, deploy_id = tracker.required_breaches(service, t, 2)
        signal = detectors[key].update(value, replay.iso(t), required_breaches=needed)
        if signal is not None:
            signal.in_deploy_window = deploy_id is not None
            signal.deploy_id = deploy_id
            signals.append(signal)
            grouper.add(signal)
        for signal in stale.check(t):
            signals.append(signal)
            grouper.add(signal)
        events += grouper.flush(t)
    events += grouper.flush(times[-1] + timedelta(days=1))
    return signals, events


print("\n#2  ALERTS BY DETECTOR (outside fault windows = false positives)")
for kind in ("ewma", "zscore", "cusum", "static"):
    signals, events = run(kind)
    sig_quiet = Counter(s.metric for s in signals if classify(parse_ts(s.timestamp)) == "quiet")
    by_class = defaultdict(list)
    for e in events:
        by_class[classify(parse_ts(e["t_detected"]))].append(e)
    fp = by_class["quiet"]
    print(f"  {kind:<7} events={len(events):>3}  in-fault={len(by_class['fault']):>3}  "
          f"after-gap={len(by_class['gap'])}  quiet-FP={len(fp)}  "
          f"FP/hour={len(fp) * 3600 / quiet_s:.2f}  "
          f"memory_bytes signals outside faults={sig_quiet.get('memory_bytes', 0)}")
    for e in fp:
        print(f"          FP {e['t_detected']} {e['services']} {e['metrics']} detector={e['detector']}")
    for e in by_class["gap"]:
        print(f"          after-gap {e['t_detected']} {len(e['services'])} services {e['metrics']}")
    if kind == "ewma":
        ewma_events = events

print("\n#2  memory_bytes behaviour per service (MB)")
series = defaultdict(list)
for t, service, metric, value in samples:
    if metric == "memory_bytes":
        series[service].append((t, value))
for service, points in sorted(series.items()):
    values = [v for _, v in points]
    steps = [b - a for (ta, a), (tb, b) in zip(points, points[1:]) if tb - ta <= GAP]
    print(f"  {service:<10} min={min(values)/1e6:7.1f}  max={max(values)/1e6:7.1f}  "
          f"biggest rise={max(steps)/1e6:6.1f}  biggest drop={min(steps)/1e6:6.1f}  "
          f"steps>=50MB={sum(1 for s in steps if abs(s) >= 50e6)}")

print("\n#3  evidence_window width (ewma events)")
widths = [(parse_ts(e["evidence_window"]["end"]) - parse_ts(e["evidence_window"]["start"])).total_seconds()
          for e in ewma_events]
print(f"  events={len(widths)}  zero-width={sum(1 for w in widths if w == 0)}  "
      f"min={min(widths):.1f}s  median={statistics.median(widths):.1f}s  max={max(widths):.1f}s")
ordered = sum(1 for e in ewma_events
              if parse_ts(e["evidence_window"]["start"]) <= parse_ts(e["t_detected"])
              <= parse_ts(e["evidence_window"]["end"]))
print(f"  start <= t_detected <= end holds for {ordered}/{len(ewma_events)}")

print("\n#4  grouping and ids (ewma events)")
ids = [e["anomaly_id"] for e in ewma_events]
print(f"  events={len(ewma_events)}  multi-metric={sum(1 for e in ewma_events if len(e['metrics']) > 1)}  "
      f"multi-service={sum(1 for e in ewma_events if len(e['services']) > 1)}  "
      f"duplicate ids={len(ids) - len(set(ids))}")
contrib = [len(e["contributors"]) for e in ewma_events]
print(f"  contributing signals per event: median={statistics.median(contrib)}  max={max(contrib)}  "
      f"(each of these would have been a separate alert before grouping)")
