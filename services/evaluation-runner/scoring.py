"""
Scoring for the evaluation harness — the numbers the final report quotes.

Deliberately pure functions over plain dataclasses, with no Kafka, database
or HTTP anywhere in this module, so every metric in the report is unit
tested rather than merely observed once during a demo.

A note on what counts as a detection. An alert that fires during a fault but
names the wrong service is not a success — the whole point of the system is
to say *which* service is at fault — but it is also not the same failure as
noticing nothing at all. The two are scored separately (`MISATTRIBUTED` vs
`MISSED`) rather than collapsed, because conflating them would flatter the
detector.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

# Metrics are derived from Prometheus `rate(...[1m])` windows, so a fault's
# effect both lags its injection and decays for about a minute after
# recovery. The scoring window is widened accordingly; without this, genuine
# detections that land just after t_recovered would be miscounted as false
# positives and the false-positive rate would be overstated.
DEFAULT_GRACE_AFTER = timedelta(seconds=90)
DEFAULT_GRACE_BEFORE = timedelta(seconds=0)


class Outcome(str, Enum):
    DETECTED = "detected"
    MISATTRIBUTED = "misattributed"
    MISSED = "missed"
    # The fault ran, but the ground-truth service emitted no request-level
    # telemetry during its window, so no detector could possibly have seen it.
    # Scored apart from MISSED because it measures the testbed, not the
    # detector — and rolled into the headline rate anyway, so the distinction
    # informs rather than excuses.
    UNOBSERVABLE = "unobservable"


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    fault_type: str
    ground_truth_service: str
    t_inject: datetime
    t_recovered: datetime | None = None
    status: str = "recovered"


@dataclass(frozen=True)
class DetectedEvent:
    anomaly_id: str
    services: tuple[str, ...]
    metrics: tuple[str, ...]
    severity: str
    t_detected: datetime
    t_onset: datetime | None = None


@dataclass
class ScenarioResult:
    scenario: Scenario
    outcome: Outcome
    detection_latency_s: float | None = None
    matched_event_id: str | None = None
    events_in_window: int = 0

    @property
    def is_correct(self) -> bool:
        return self.outcome is Outcome.DETECTED


def parse_ts(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def to_detected_event(payload: dict) -> DetectedEvent | None:
    """Parse an `anomalies.detected` payload, or None if it is malformed."""
    try:
        return DetectedEvent(
            anomaly_id=payload["anomaly_id"],
            services=tuple(payload.get("services", ())),
            metrics=tuple(payload.get("metrics", ())),
            severity=payload.get("severity", "low"),
            t_detected=parse_ts(payload["t_detected"]),
            t_onset=parse_ts(payload["t_onset"]) if payload.get("t_onset") else None,
        )
    except (KeyError, ValueError):
        return None


def fault_window(
    scenario: Scenario,
    grace_before: timedelta = DEFAULT_GRACE_BEFORE,
    grace_after: timedelta = DEFAULT_GRACE_AFTER,
) -> tuple[datetime, datetime]:
    """The span during which an alert is plausibly caused by this fault."""
    end = scenario.t_recovered or scenario.t_inject
    return scenario.t_inject - grace_before, end + grace_after


def score_scenario(
    scenario: Scenario,
    events: list[DetectedEvent],
    grace_before: timedelta = DEFAULT_GRACE_BEFORE,
    grace_after: timedelta = DEFAULT_GRACE_AFTER,
    observable: bool = True,
) -> ScenarioResult:
    """Score one injected fault against everything the detector emitted.

    `observable` is False when the ground-truth service produced no
    request-level telemetry during the window. A detection still counts as a
    detection in that case; only a non-detection is reclassified, because
    there was nothing available to detect.
    """
    start, end = fault_window(scenario, grace_before, grace_after)
    in_window = sorted(
        (e for e in events if start <= e.t_detected <= end),
        key=lambda e: e.t_detected,
    )

    if not in_window:
        return ScenarioResult(
            scenario=scenario,
            outcome=Outcome.MISSED if observable else Outcome.UNOBSERVABLE,
        )

    correct = [e for e in in_window if scenario.ground_truth_service in e.services]
    if not correct:
        return ScenarioResult(
            scenario=scenario,
            outcome=Outcome.MISATTRIBUTED if observable else Outcome.UNOBSERVABLE,
            matched_event_id=in_window[0].anomaly_id,
            events_in_window=len(in_window),
        )

    first = correct[0]
    return ScenarioResult(
        scenario=scenario,
        outcome=Outcome.DETECTED,
        detection_latency_s=(first.t_detected - scenario.t_inject).total_seconds(),
        matched_event_id=first.anomaly_id,
        events_in_window=len(in_window),
    )


def find_false_positives(
    events: list[DetectedEvent],
    scenarios: list[Scenario],
    grace_before: timedelta = DEFAULT_GRACE_BEFORE,
    grace_after: timedelta = DEFAULT_GRACE_AFTER,
) -> list[DetectedEvent]:
    """Alerts that fired while no fault was running."""
    windows = [fault_window(s, grace_before, grace_after) for s in scenarios]
    return [
        e for e in events
        if not any(start <= e.t_detected <= end for start, end in windows)
    ]


def quiet_seconds(
    observed_from: datetime,
    observed_until: datetime,
    scenarios: list[Scenario],
    grace_before: timedelta = DEFAULT_GRACE_BEFORE,
    grace_after: timedelta = DEFAULT_GRACE_AFTER,
) -> float:
    """Observed time with no fault running, as the FP-rate denominator.

    Overlapping fault windows are merged before subtracting, so concurrent
    or back-to-back scenarios cannot discount the same second twice and
    produce a misleadingly small denominator.
    """
    total = (observed_until - observed_from).total_seconds()
    if total <= 0:
        return 0.0

    spans = sorted(
        (
            (max(start, observed_from), min(end, observed_until))
            for start, end in (
                fault_window(s, grace_before, grace_after) for s in scenarios
            )
        ),
        key=lambda s: s[0],
    )

    merged: list[list[datetime]] = []
    for start, end in spans:
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    busy = sum((end - start).total_seconds() for start, end in merged)
    return max(total - busy, 0.0)


def false_positives_per_hour(false_positive_count: int, quiet_s: float) -> float | None:
    """None rather than a divide-by-zero when nothing quiet was observed."""
    if quiet_s <= 0:
        return None
    return false_positive_count * 3600.0 / quiet_s


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile; small sample sizes make interpolation noise."""
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(pct / 100 * len(ordered) + 0.5) - 1))
    return ordered[index]


def reciprocal_rank(ranked_services: list[str], ground_truth_service: str) -> float:
    """1/rank of the correct service, or 0 if it never appears."""
    for position, service in enumerate(ranked_services, start=1):
        if service == ground_truth_service:
            return 1.0 / position
    return 0.0


def mean_reciprocal_rank(rankings: list[tuple[list[str], str]]) -> float | None:
    """MRR over (ranked_services, ground_truth) pairs.

    Consumed by the M3 ablation. Returns None for an empty set rather than
    reporting a confident 0.0 for a comparison that was never run.
    """
    if not rankings:
        return None
    return statistics.fmean(
        reciprocal_rank(ranked, truth) for ranked, truth in rankings
    )


def top_k_accuracy(rankings: list[tuple[list[str], str]], k: int) -> float | None:
    if not rankings:
        return None
    hits = sum(1 for ranked, truth in rankings if truth in ranked[:k])
    return hits / len(rankings)


@dataclass
class Summary:
    fault_type: str
    total: int
    detected: int
    misattributed: int
    missed: int
    unobservable: int = 0
    latencies: list[float] = field(default_factory=list)

    @property
    def detection_rate(self) -> float:
        """Over every scenario attempted — the number that cannot flatter."""
        return self.detected / self.total if self.total else 0.0

    @property
    def observable(self) -> int:
        return self.total - self.unobservable

    @property
    def detection_rate_observable(self) -> float | None:
        """Over scenarios that produced something detectable.

        Reported alongside `detection_rate`, never instead of it: quoting only
        this one would let a broken testbed masquerade as a good detector.
        """
        return self.detected / self.observable if self.observable else None

    @property
    def median_latency_s(self) -> float | None:
        return statistics.median(self.latencies) if self.latencies else None

    @property
    def p95_latency_s(self) -> float | None:
        return percentile(self.latencies, 95)


def summarize(results: list[ScenarioResult]) -> dict[str, Summary]:
    """Per-fault-type rollup, plus an "overall" row."""
    summaries: dict[str, Summary] = {}

    for result in results:
        for bucket in (result.scenario.fault_type, "overall"):
            summary = summaries.setdefault(
                bucket, Summary(fault_type=bucket, total=0, detected=0,
                                misattributed=0, missed=0)
            )
            summary.total += 1
            if result.outcome is Outcome.DETECTED:
                summary.detected += 1
                if result.detection_latency_s is not None:
                    summary.latencies.append(result.detection_latency_s)
            elif result.outcome is Outcome.MISATTRIBUTED:
                summary.misattributed += 1
            elif result.outcome is Outcome.UNOBSERVABLE:
                summary.unobservable += 1
            else:
                summary.missed += 1

    return summaries
