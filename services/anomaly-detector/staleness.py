"""
Liveness detection — catching the fault that produces no data at all.

Every detector in `detectors.py` judges a sample. That makes all of them
structurally blind to a hard outage, because a crashed service does not
report bad numbers, it stops reporting entirely: `metrics-bridge` asks
Prometheus for targets whose health is `up` and skips the rest, so a stopped
container simply vanishes from `metrics.raw`. Nothing arrives, nothing is
evaluated, no alert fires.

This was not theoretical. The first live evaluation run detected a CPU-throttle
fault in 28s and missed a `service_crash` completely — zero alerts — and the
metrics table showed a 50-second hole where `payment` should have been.

So silence has to be a signal in its own right. This monitor is driven by the
clock rather than by arriving samples, and reports a service that was
reporting regularly and then stopped.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from detectors import Signal
from grouping import format_ts


class StalenessMonitor:
    """Reports services that have gone quiet.

    Tracked per service rather than per (service, metric): when a container
    dies every one of its metrics stops together, so per-metric tracking
    would produce six identical signals about one dead process.
    """

    def __init__(
        self,
        stale_after_seconds: float = 30.0,
        min_observations: int = 3,
    ):
        self.stale_after = timedelta(seconds=stale_after_seconds)
        self.min_observations = min_observations
        self._last_seen: dict[str, datetime] = {}
        self._observations: dict[str, int] = {}
        self._reported: set[str] = set()

    def observe(self, service: str, at: datetime) -> None:
        previous = self._last_seen.get(service)
        if previous is None or at > previous:
            self._last_seen[service] = at
        self._observations[service] = self._observations.get(service, 0) + 1
        # The service is reporting again, so re-arm it for the next outage.
        self._reported.discard(service)

    def check(self, now: datetime) -> list[Signal]:
        """Emit one signal per newly-silent service."""
        signals = []
        for service, last_seen in self._last_seen.items():
            if service in self._reported:
                continue
            # Never alarm on a service seen once and never again; it was
            # probably mid-startup rather than mid-outage.
            if self._observations.get(service, 0) < self.min_observations:
                continue

            gap = now - last_seen
            if gap < self.stale_after:
                continue

            self._reported.add(service)
            signals.append(
                Signal(
                    service=service,
                    metric="liveness",
                    value=gap.total_seconds(),
                    baseline=self.stale_after.total_seconds(),
                    score=gap.total_seconds() / self.stale_after.total_seconds(),
                    severity="high",
                    timestamp=format_ts(now),
                    detector="staleness",
                    # The outage began when the data stopped, not when the
                    # timeout expired — this keeps t_onset honest.
                    onset_timestamp=format_ts(last_seen),
                )
            )
        return signals
