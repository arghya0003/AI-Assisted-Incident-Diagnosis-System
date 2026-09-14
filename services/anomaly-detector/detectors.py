"""
Pluggable per-(service, metric) drift detectors.

Every detector implements the same `update(value, timestamp) -> Signal | None`
interface so the evaluation runner can swap one for another over an identical
replayed metric stream. That swap-ability is the whole point: the headline
result this project has to defend is "EWMA earned its place over a static
threshold", which is only measurable if both run against the same input.

Detectors emit a raw `Signal` per (service, metric). Turning many signals into
one grouped incident is `grouping.py`'s job, not theirs.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass


SEVERITIES = ("low", "medium", "high")

# Smallest change in a metric that is worth waking anyone for, regardless of
# how statistically striking it looks. A metric resting at or near zero — a
# healthy error_rate — has almost no variance, so a microscopic wobble scores
# an enormous z. Statistical significance and practical significance are
# different questions, and a detector needs to ask both.
MIN_DEVIATION: dict[str, float] = {
    "error_rate": 0.01,        # one percentage point
    "latency_p50_ms": 10.0,
    "latency_p95_ms": 20.0,
    "latency_p99_ms": 25.0,
    "cpu_rate": 0.05,
    "memory_bytes": 50e6,      # 50 MB
}

# Metrics where only an increase is an incident. Faster responses and fewer
# errors are good news, and treating them as anomalies is actively harmful:
# after a restart a service is slow while it boots, the detector learns that
# as its baseline, and latency settling back to normal then reads as a huge
# deviation. That false alarm is not just noise — its cooldown went on to
# swallow a real 362 ms fault on 2026-09-14. cpu_rate and memory_bytes stay
# two-sided, because a sudden fall there usually means the process restarted.
UPWARD_ONLY_METRICS = frozenset({
    "latency_p50_ms", "latency_p95_ms", "latency_p99_ms", "error_rate",
})


@dataclass
class Signal:
    """One (service, metric) breach, before dedup/grouping."""

    service: str
    metric: str
    value: float
    baseline: float
    score: float
    severity: str
    timestamp: str
    detector: str
    # When the breach streak began, which is earlier than `timestamp` whenever
    # the detector waited for corroborating samples before firing.
    onset_timestamp: str | None = None
    in_deploy_window: bool = False
    deploy_id: str | None = None


def severity_from_ratio(ratio: float) -> str:
    """Map "how far past the firing threshold" onto the severity enum.

    Expressed as a ratio rather than a raw score so every detector maps onto
    the same enum, even though a z-score, a CUSUM statistic and a threshold
    overshoot are not otherwise comparable.
    """
    if ratio >= 2.0:
        return "high"
    if ratio >= 1.3:
        return "medium"
    return "low"


class Detector(ABC):
    """Base class holding the machinery every detector shares.

    Subclasses implement `_evaluate` (is this sample a breach, and how badly)
    and `_observe` (fold a sample into the baseline). The base class owns
    warm-up, consecutive-breach gating and baseline freezing, so those
    behaviours stay identical across detectors and don't quietly become a
    confound in the ablation.
    """

    name = "base"

    def __init__(
        self,
        service: str,
        metric: str,
        warmup: int = 10,
        required_breaches: int = 2,
        freeze_on_breach: bool = True,
        max_frozen: int = 12,
        min_deviation: float | None = None,
        upward_only: bool | None = None,
    ):
        self.service = service
        self.metric = metric
        self.warmup = warmup
        self.min_deviation = (
            MIN_DEVIATION.get(metric, 0.0) if min_deviation is None else min_deviation
        )
        self.upward_only = (
            metric in UPWARD_ONLY_METRICS if upward_only is None else upward_only
        )
        self.required_breaches = required_breaches
        self.freeze_on_breach = freeze_on_breach
        self.max_frozen = max_frozen

        self._count = 0
        self._consecutive_breaches = 0
        self._frozen_for = 0
        self._streak_started_at: str | None = None

    @property
    def warm(self) -> bool:
        return self._count >= self.warmup

    @abstractmethod
    def _evaluate(self, value: float) -> tuple[bool, float, float]:
        """Return (is_breach, ratio_over_threshold, baseline)."""

    @abstractmethod
    def _observe(self, value: float) -> None:
        """Fold a sample into the baseline."""

    def _on_fire(self) -> None:
        """Hook for state a detector should clear once a signal is emitted.

        Deliberately not called when a breach is swallowed by the
        corroboration gate: a detector that accumulates evidence must not
        have that evidence discarded for a signal that was never sent.
        """

    def update(
        self,
        value: float,
        timestamp: str,
        required_breaches: int | None = None,
    ) -> Signal | None:
        """Feed one sample in; get a Signal back if it fires.

        `required_breaches` overrides the configured value for this sample
        only — the deploy-window policy uses it to demand more corroboration
        while a service is mid-deploy.
        """
        if not self.warm:
            self._observe(value)
            self._count += 1
            return None

        is_breach, ratio, baseline = self._evaluate(value)
        if is_breach and abs(value - baseline) < self.min_deviation:
            is_breach = False
        # Not a breach, so the sample is observed below and the baseline
        # follows the metric down rather than staying anchored at boot level.
        if is_breach and self.upward_only and value <= baseline:
            is_breach = False
        needed = self.required_breaches if required_breaches is None else required_breaches

        if not is_breach:
            self._consecutive_breaches = 0
            self._frozen_for = 0
            self._streak_started_at = None
            self._observe(value)
            self._count += 1
            return None

        if self._consecutive_breaches == 0:
            self._streak_started_at = timestamp
        self._consecutive_breaches += 1

        # A breaching sample normally must not pollute the baseline, or a
        # sustained fault silently becomes the new normal and the detector
        # goes quiet mid-incident. The max_frozen cap stops that from
        # freezing the baseline forever against a genuine level shift.
        if self.freeze_on_breach and self._frozen_for < self.max_frozen:
            self._frozen_for += 1
        else:
            self._observe(value)

        self._count += 1

        if self._consecutive_breaches < needed:
            return None

        self._on_fire()
        return Signal(
            service=self.service,
            metric=self.metric,
            value=value,
            baseline=baseline,
            score=ratio,
            severity=severity_from_ratio(ratio),
            timestamp=timestamp,
            detector=self.name,
            onset_timestamp=self._streak_started_at,
        )


class _EWMABaseline:
    """Exponentially weighted mean and variance with a scale-free noise floor.

    The noise floor is relative to the mean, not an absolute constant. An
    absolute floor silently breaks any metric whose natural scale is small:
    `error_rate` lives in [0, 1], so an absolute floor of 0.5 puts a 3-sigma
    breach at an error rate above 1.5 — unreachable, i.e. error-rate anomalies
    could never fire at all.
    """

    def __init__(self, alpha: float = 0.2, rel_floor: float = 0.02, abs_floor: float = 1e-9):
        self.alpha = alpha
        self.rel_floor = rel_floor
        self.abs_floor = abs_floor
        self.mean: float | None = None
        self.var: float = 0.0

    def observe(self, value: float) -> None:
        if self.mean is None:
            self.mean = value
            self.var = 0.0
            return
        prev = self.mean
        self.mean = self.alpha * value + (1 - self.alpha) * prev
        self.var = self.alpha * (value - prev) ** 2 + (1 - self.alpha) * self.var

    def std(self) -> float:
        raw = math.sqrt(max(self.var, 0.0))
        mean = abs(self.mean or 0.0)
        return max(raw, self.rel_floor * mean, self.abs_floor)


class EWMADetector(Detector):
    """EWMA drift detection — the primary detector this project argues for."""

    name = "ewma"

    def __init__(self, service: str, metric: str, alpha: float = 0.2,
                 z_threshold: float = 3.0, **kwargs):
        super().__init__(service, metric, **kwargs)
        self.z_threshold = z_threshold
        self._baseline = _EWMABaseline(alpha=alpha)

    def _evaluate(self, value: float) -> tuple[bool, float, float]:
        mean = self._baseline.mean or 0.0
        z = abs(value - mean) / self._baseline.std()
        return z >= self.z_threshold, z / self.z_threshold, mean

    def _observe(self, value: float) -> None:
        self._baseline.observe(value)


class ThreeSigmaDetector(Detector):
    """Rolling-window 3-sigma z-score — the classic textbook comparison."""

    name = "zscore"

    def __init__(self, service: str, metric: str, window: int = 60,
                 z_threshold: float = 3.0, rel_floor: float = 0.02, **kwargs):
        super().__init__(service, metric, **kwargs)
        self.z_threshold = z_threshold
        self.rel_floor = rel_floor
        self._window: deque[float] = deque(maxlen=window)

    def _stats(self) -> tuple[float, float]:
        n = len(self._window)
        mean = sum(self._window) / n
        var = sum((v - mean) ** 2 for v in self._window) / max(n - 1, 1)
        std = max(math.sqrt(var), self.rel_floor * abs(mean), 1e-9)
        return mean, std

    def _evaluate(self, value: float) -> tuple[bool, float, float]:
        mean, std = self._stats()
        z = abs(value - mean) / std
        return z >= self.z_threshold, z / self.z_threshold, mean

    def _observe(self, value: float) -> None:
        self._window.append(value)


class CUSUMDetector(Detector):
    """Two-sided tabular CUSUM on standardised deviations.

    Accumulates small persistent shifts, so it catches slow drifts (a memory
    leak) that a per-sample z-score never trips on.
    """

    name = "cusum"

    def __init__(self, service: str, metric: str, alpha: float = 0.2,
                 k: float = 0.5, h: float = 5.0, **kwargs):
        # The CUSUM statistic already encodes persistence — that is the whole
        # reason to use one. Demanding consecutive crossings on top of it
        # double-counts the same requirement and, because each crossing
        # clears the accumulator, makes a slow drift undetectable.
        kwargs.setdefault("required_breaches", 1)
        super().__init__(service, metric, **kwargs)
        self.k = k
        self.h = h
        self._baseline = _EWMABaseline(alpha=alpha)
        self._s_hi = 0.0
        self._s_lo = 0.0

    def _evaluate(self, value: float) -> tuple[bool, float, float]:
        mean = self._baseline.mean or 0.0
        z = (value - mean) / self._baseline.std()
        self._s_hi = max(0.0, self._s_hi + z - self.k)
        # Left accumulating, the downward sum would bank a past improvement
        # and add it to whatever small rise came next.
        self._s_lo = 0.0 if self.upward_only else max(0.0, self._s_lo - z - self.k)

        stat = max(self._s_hi, self._s_lo)
        return stat > self.h, stat / self.h, mean

    def _on_fire(self) -> None:
        # Cleared only once a signal actually goes out, so one shift produces
        # one alarm rather than a permanently latched statistic.
        self._s_hi = self._s_lo = 0.0

    def _observe(self, value: float) -> None:
        self._baseline.observe(value)


# Hand-configured limits, i.e. what an engineer would actually put in an
# alerting rule without any statistics. This is the honest baseline EWMA has
# to beat; picking absurd values here would make the ablation meaningless.
STATIC_THRESHOLDS: dict[str, float] = {
    "latency_p50_ms": 200.0,
    "latency_p95_ms": 500.0,
    "latency_p99_ms": 1000.0,
    "error_rate": 0.05,
    "cpu_rate": 0.90,
    "memory_bytes": 1.5e9,
}


class StaticThresholdDetector(Detector):
    """Fixed per-metric limits, with no learned baseline at all."""

    name = "static"

    def __init__(self, service: str, metric: str,
                 thresholds: dict[str, float] | None = None, **kwargs):
        # No baseline to learn, so no warm-up is needed — forcing one would
        # hand EWMA an unearned latency advantage in the ablation. The
        # threshold is already an absolute limit, so the practical-significance
        # gate would double-count.
        kwargs.setdefault("warmup", 0)
        kwargs.setdefault("min_deviation", 0.0)
        super().__init__(service, metric, **kwargs)
        self.threshold = (thresholds or STATIC_THRESHOLDS).get(metric)

    def _evaluate(self, value: float) -> tuple[bool, float, float]:
        if self.threshold is None:
            return False, 0.0, 0.0
        return value > self.threshold, value / self.threshold, self.threshold

    def _observe(self, value: float) -> None:
        return None


DETECTOR_TYPES: dict[str, type[Detector]] = {
    "ewma": EWMADetector,
    "zscore": ThreeSigmaDetector,
    "cusum": CUSUMDetector,
    "static": StaticThresholdDetector,
}


def build_detector(kind: str, service: str, metric: str, **kwargs) -> Detector:
    if kind not in DETECTOR_TYPES:
        raise ValueError(f"unknown detector {kind!r}, expected one of {sorted(DETECTOR_TYPES)}")
    return DETECTOR_TYPES[kind](service=service, metric=metric, **kwargs)
