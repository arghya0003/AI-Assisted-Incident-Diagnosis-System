"""
Anomaly deduplication and grouping.

One fault trips many metrics across many services: a CPU-throttled
`catalogue` pushes p50/p95/p99 latency and error rate on `catalogue`, and
then on `front-end` as the timeout propagates. Emitting one event per
(service, metric) breach would put a dozen near-identical alerts on
`anomalies.detected`, which is both useless to a human and expensive for the
LLM context budget downstream. This module collapses them into a single
event carrying a member list.

Two timing decisions worth knowing about:

  * An event is emitted `group_delay` after the group's FIRST signal, not
    after its last. Waiting for the fault to go quiet would mean a 300s fault
    produces its alert 300s late; anchoring on the first signal bounds
    emission latency at `group_delay` no matter how long the fault runs.
  * `t_detected` is stamped from the first signal, not from emission time, so
    the grouping delay does not inflate the detection-latency number the
    evaluation runner measures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from detectors import SEVERITIES, Signal


def parse_ts(value: str) -> datetime:
    """Parse the Zulu-suffixed ISO timestamps used across the contracts."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def format_ts(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def max_severity(values) -> str:
    return max(values, key=SEVERITIES.index, default="low")


@dataclass
class Group:
    """An in-flight incident: every signal believed to share one root cause."""

    opened_at: datetime
    emit_at: datetime
    signals: list[Signal] = field(default_factory=list)

    @property
    def services(self) -> list[str]:
        return sorted({s.service for s in self.signals})

    @property
    def metrics(self) -> list[str]:
        return sorted({s.metric for s in self.signals})

    @property
    def severity(self) -> str:
        return max_severity(s.severity for s in self.signals)

    @property
    def t_detected(self) -> datetime:
        return min(parse_ts(s.timestamp) for s in self.signals)

    @property
    def t_onset(self) -> datetime:
        return min(parse_ts(s.onset_timestamp or s.timestamp) for s in self.signals)

    @property
    def last_seen(self) -> datetime:
        return max(parse_ts(s.timestamp) for s in self.signals)

    def add(self, signal: Signal) -> None:
        self.signals.append(signal)


class AnomalyGrouper:
    """Collects raw signals and yields one grouped event per incident.

    Grouping is purely time-based: signals landing inside the same short
    window are treated as one incident. That is the right call for this
    testbed, where the evaluation harness injects one fault at a time. The
    known limitation — two genuinely unrelated faults overlapping in time
    would merge into one event — is documented rather than silently ignored,
    and is why `services[]` is a list the consumer can inspect.

    A cooling service is muted only against signals of similar size. One far
    worse than the incident that started the mute — `escalation_factor` times
    its peak score — opens a new incident instead. Without that, a minor
    alert can hide a major one: on 2026-09-14 a startup blip scoring ~2.6
    muted catalogue, and a 362 ms regression scoring ~18.6 two minutes later
    was silently absorbed as the same incident.
    """

    def __init__(
        self,
        group_delay_seconds: float = 15.0,
        cooldown_seconds: float = 120.0,
        max_members: int = 50,
        escalation_factor: float = 3.0,
    ):
        self.group_delay = timedelta(seconds=group_delay_seconds)
        self.cooldown = timedelta(seconds=cooldown_seconds)
        self.max_members = max_members
        self.escalation_factor = escalation_factor

        self._open: Group | None = None
        # service -> time the cooling period for that service expires
        self._cooling: dict[str, datetime] = {}
        # service -> peak score of the incident that started its cooldown
        self._cooling_peak: dict[str, float] = {}
        self._sequence = 0

    def _next_anomaly_id(self, when: datetime) -> str:
        self._sequence += 1
        return f"anom-{when.strftime('%Y%m%dT%H%M%S')}-{self._sequence:04d}"

    def _is_cooling(self, service: str, now: datetime) -> bool:
        expires = self._cooling.get(service)
        return expires is not None and now < expires

    def add(self, signal: Signal) -> None:
        """Buffer one raw signal. Never emits; call `flush` for that."""
        now = parse_ts(signal.timestamp)

        # Expire stale cooldowns so a service isn't muted forever.
        self._cooling = {svc: exp for svc, exp in self._cooling.items() if exp > now}
        self._cooling_peak = {
            svc: peak for svc, peak in self._cooling_peak.items() if svc in self._cooling
        }

        if self._open is not None:
            if len(self._open.signals) < self.max_members:
                self._open.add(signal)
            return

        if self._is_cooling(signal.service, now):
            peak = self._cooling_peak.get(signal.service, float("inf"))
            if signal.score < self.escalation_factor * peak:
                # Same fault still going. Extend the mute rather than re-alerting.
                self._cooling[signal.service] = now + self.cooldown
                return
            # Far worse than what started the mute: a new incident, not the
            # old one continuing. Falls through to open a fresh group.

        self._open = Group(opened_at=now, emit_at=now + self.group_delay)
        self._open.add(signal)

    def flush(self, now: datetime) -> list[dict]:
        """Emit any group whose grouping delay has elapsed.

        `now` is passed in rather than read from the clock so replaying a
        recorded metric stream produces byte-identical results.
        """
        if self._open is None or now < self._open.emit_at:
            return []

        group = self._open
        self._open = None

        for service in group.services:
            self._cooling[service] = max(group.last_seen, now) + self.cooldown
            self._cooling_peak[service] = max(
                s.score for s in group.signals if s.service == service
            )

        return [self._to_event(group)]

    def _to_event(self, group: Group) -> dict:
        detected = group.t_detected
        contributors = [
            {
                "service": s.service,
                "metric": s.metric,
                "value": s.value,
                "baseline": s.baseline,
                "score": round(s.score, 4),
                "severity": s.severity,
                "observed_at": s.timestamp,
            }
            for s in group.signals
        ]
        in_deploy_window = any(s.in_deploy_window for s in group.signals)
        deploy_ids = sorted({s.deploy_id for s in group.signals if s.deploy_id})

        event = {
            "anomaly_id": self._next_anomaly_id(detected),
            "services": group.services,
            "metrics": group.metrics,
            "severity": group.severity,
            "t_detected": format_ts(detected),
            "t_onset": format_ts(group.t_onset),
            "evidence_window": {
                "start": format_ts(group.t_onset),
                "end": format_ts(group.last_seen),
            },
            # Beyond the frozen contract: M3 gets the per-metric detail it
            # needs to build evidence items without re-querying TimescaleDB.
            "detector": group.signals[0].detector,
            "contributors": contributors,
            "in_deploy_window": in_deploy_window,
        }
        if deploy_ids:
            event["related_deploy_ids"] = deploy_ids
        return event
