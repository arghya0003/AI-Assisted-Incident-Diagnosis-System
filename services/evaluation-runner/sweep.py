"""
Scoring for the severity sweep: how does the detector comparison change as the
injected fault gets harder to miss?

The project's headline detection claim is that a learned baseline beats a fixed
threshold. Measured twice at different fault magnitudes it came out 6/7 vs 4/7
once and 7/9 vs 7/9 the other time, which makes fault severity an experimental
variable the evaluation never controlled (issue #34). Quoting either run alone
would be misleading: the first overstates the benefit by choosing a fault the
threshold happens to miss, the second understates it by choosing one so blatant
that nothing could miss it.

The honest result is the relationship between them, and the number that states
it is the **crossover**: the fault magnitude at which a fixed threshold starts
catching up. Below it the learned baseline earns its place; above it the two are
indistinguishable.

Everything here is pure, so the curve can be scored from recorded numbers
without a stack. The measuring lives in `sources.py`, the running in `main.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# A fixed threshold can only be compared against a learned one on a metric that
# actually has a fixed threshold configured. latency_p95_ms is the one the
# ablation has always turned on, and it is the metric a CPU-throttle moves.
SWEEP_METRIC = "latency_p95_ms"

# The detector whose threshold is fixed. Everything else in a sweep is adaptive
# and is compared against it as a group.
STATIC_DETECTOR = "static"


@dataclass
class SeverityPoint:
    """One rung of the sweep: a fault magnitude and what each detector made of it."""

    cpu_limit: float
    baseline_p95_ms: float | None
    fault_p95_ms: float | None
    scenario_ids: list[str] = field(default_factory=list)
    # detector -> (detected, total)
    outcomes: dict[str, tuple[int, int]] = field(default_factory=dict)

    @property
    def impact_ms(self) -> float | None:
        """How much the fault actually moved p95, in milliseconds.

        This is what the curve is plotted against, not `cpu_limit`. A quota of
        0.002 is a near no-op on an idle service and crippling on a busy one, so
        cpu_limit is not comparable across services or across days — measured
        impact is.
        """
        if self.baseline_p95_ms is None or self.fault_p95_ms is None:
            return None
        return self.fault_p95_ms - self.baseline_p95_ms

    def rate(self, detector: str) -> float | None:
        detected, total = self.outcomes.get(detector, (0, 0))
        return detected / total if total else None

    @property
    def observed(self) -> bool:
        """Whether this rung measured anything worth putting on the curve."""
        return self.impact_ms is not None and any(t for _, t in self.outcomes.values())


def adaptive_detectors(point: SeverityPoint) -> list[str]:
    return [d for d in point.outcomes if d != STATIC_DETECTOR]


def best_adaptive_rate(point: SeverityPoint) -> float | None:
    """The strongest adaptive detector at this magnitude.

    Best rather than mean: the claim under test is "a learned baseline can do
    what a fixed threshold cannot", so the comparison is against the best one
    available. Averaging in a weaker adaptive detector would make the fixed
    threshold look better than it is.
    """
    rates = [point.rate(d) for d in adaptive_detectors(point)]
    rates = [r for r in rates if r is not None]
    return max(rates) if rates else None


def crossover(points: list[SeverityPoint]) -> SeverityPoint | None:
    """The smallest measured impact at which the fixed threshold has caught up.

    "Caught up" means its detection rate is at least the best adaptive rate - at
    that magnitude the learned baseline buys nothing. Returns None when it never
    catches up in the swept range, which is itself a result: the sweep did not
    reach a fault obvious enough for a fixed threshold, and the curve should be
    extended before claiming a crossover exists.

    Points are ordered by measured impact, not by cpu_limit, because the two are
    not guaranteed to be monotonic: a service can be busier on one run than
    another, so a lower quota does not always produce a bigger impact.
    """
    usable = [p for p in points if p.observed]
    usable.sort(key=lambda p: p.impact_ms)
    for point in usable:
        static = point.rate(STATIC_DETECTOR)
        adaptive = best_adaptive_rate(point)
        if static is None or adaptive is None:
            continue
        if static >= adaptive:
            return point
    return None


def separation(points: list[SeverityPoint]) -> list[tuple[SeverityPoint, float]]:
    """How much the learned baseline wins by at each magnitude, biggest first.

    The top of this list is the most useful single scenario in the suite: it is
    the fault where the choice of detector decides whether the incident is seen
    at all.
    """
    out = []
    for point in points:
        static = point.rate(STATIC_DETECTOR)
        adaptive = best_adaptive_rate(point)
        if static is None or adaptive is None:
            continue
        out.append((point, adaptive - static))
    out.sort(key=lambda pair: -pair[1])
    return out


def verdict(points: list[SeverityPoint]) -> str:
    """One sentence stating what the sweep showed, for the report.

    Written here rather than in the renderer so the wording is covered by a
    test: this sentence is the finding, and it should not be able to drift into
    claiming more than the numbers support.
    """
    usable = [p for p in points if p.observed]
    if len(usable) < 2:
        return ("Not enough rungs produced a measurable impact to draw a curve; "
                "the sweep measured the testbed rather than the detectors.")

    gaps = separation(usable)
    if not gaps:
        return "No rung could be compared: the static detector produced no scored result."

    widest, margin = gaps[0]
    point = crossover(usable)
    if margin <= 0:
        return ("A fixed threshold matched the learned baseline at every magnitude swept, "
                "so these runs show no benefit from a learned baseline on this metric.")
    if point is None:
        return (
            f"A learned baseline led by up to {margin:.0%} at {widest.impact_ms:.0f} ms of "
            "impact, and the fixed threshold never caught up within the range swept - so "
            "the crossover is above the largest fault measured, not absent."
        )
    return (
        f"A learned baseline led by up to {margin:.0%} at {widest.impact_ms:.0f} ms of "
        f"impact, and the fixed threshold caught up by {point.impact_ms:.0f} ms. A learned "
        "baseline earns its place on subtle regressions and makes no difference once a fault "
        "is severe enough for any threshold to catch - which is the honest form of the claim."
    )
