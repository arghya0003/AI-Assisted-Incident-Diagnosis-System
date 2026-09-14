"""
The labelled fault suite the evaluation runs against.

Only the three fault types the injector can physically produce are listed.
The project plan names six classes; memory leak / OOM, dependency timeout
cascade and config error are not implemented in `services/fault-injector`
yet, so they are absent here rather than represented by a scenario that
quietly does nothing. See docs/phase9-detection.md for that gap.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ScenarioSpec:
    fault_type: str
    service: str
    duration_s: int
    params: dict = field(default_factory=dict)

    def request_body(self) -> dict:
        return {
            "fault_type": self.fault_type,
            "service": self.service,
            "duration_s": self.duration_s,
            **self.params,
        }

    @property
    def label(self) -> str:
        return f"{self.fault_type}/{self.service}"


# Durations are long enough for a 1m-rate-window metric to actually move —
# a 10s fault is invisible to a p95 computed over a 1m window, so scoring
# one would measure the metrics pipeline's resolution, not the detector.
DEFAULT_SUITE: list[ScenarioSpec] = [
    ScenarioSpec("bad_deploy_latency", "catalogue", 90, {"cpu_limit": 0.05}),
    ScenarioSpec("bad_deploy_latency", "front-end", 90, {"cpu_limit": 0.05}),
    ScenarioSpec("bad_deploy_latency", "orders", 90, {"cpu_limit": 0.10}),
    ScenarioSpec("service_crash", "payment", 60),
    ScenarioSpec("service_crash", "user", 60),
    ScenarioSpec("service_crash", "shipping", 60),
    # Must exceed catalogue-db's max_connections=151 to actually saturate the
    # pool. At 100 the fault provably did nothing — catalogue's p95 never moved.
    ScenarioSpec("db_pool_saturation", "catalogue", 90, {"connections": 155}),
]

# A quick pass for wiring checks, so nobody waits 20 minutes to find out the
# consumer was misconfigured.
SMOKE_SUITE: list[ScenarioSpec] = [
    ScenarioSpec("bad_deploy_latency", "catalogue", 60, {"cpu_limit": 0.05}),
    ScenarioSpec("service_crash", "payment", 45),
]

SUITES: dict[str, list[ScenarioSpec]] = {
    "default": DEFAULT_SUITE,
    "smoke": SMOKE_SUITE,
}
