"""
The labelled fault suite the evaluation runs against.

`default` holds the three original fault types. `full` is the six-class
suite the project plan calls for; the four extra fault types landed in
`services/fault-injector` with issue #35, and stay in their own suite until a
full run has scored them, so `default`'s history stays comparable. The runner
asks the injector what it supports before a run and reports anything it
cannot produce separately (see `sources.supported_fault_types`), so an older
injector image still runs `full` safely.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Mirrors KNOWN_SERVICES in services/fault-injector/main.py. Duplicated
# deliberately: a scenario naming a service the injector will reject should
# fail in a unit test, not 20 minutes into a live run.
KNOWN_SERVICES = frozenset({
    "front-end", "catalogue", "payment", "user", "carts", "orders", "shipping",
})

# The injector's own safety cap (MAX_DURATION_SECONDS). A longer duration is
# silently truncated, which would make the recorded fault window a lie.
MAX_DURATION_S = 300

# Params each fault type needs beyond duration_s. The injector supplies
# defaults for all of them, but a scenario is a ground-truth record: the
# severity it ran at belongs in the suite where it can be read and changed,
# not in the injector's fallbacks.
REQUIRED_PARAMS: dict[str, tuple[str, ...]] = {
    "bad_deploy_latency": ("cpu_limit",),
    "service_crash": (),
    # A table lock now (issue #35); there is no severity knob to record.
    "db_pool_saturation": (),
    "dependency_timeout": ("dependency",),
    "config_error": ("dependency",),
    "memory_exhaustion": ("limit_mb",),
    "resource_exhaustion": ("cpu_limit", "steps"),
}


@dataclass(frozen=True)
class ScenarioSpec:
    fault_type: str
    service: str
    duration_s: int
    params: dict = field(default_factory=dict)

    def request_body(self) -> dict:
        # params first, envelope last: fault_type, service and duration_s are
        # the ground truth the whole run is scored against, so a stray key in
        # params must not be able to redirect the fault to another service.
        return {
            **self.params,
            "fault_type": self.fault_type,
            "service": self.service,
            "duration_s": self.duration_s,
        }

    @property
    def label(self) -> str:
        return f"{self.fault_type}/{self.service}"


# Durations are long enough for a 1m-rate-window metric to actually move —
# a 10s fault is invisible to a p95 computed over a 1m window, so scoring
# one would measure the metrics pipeline's resolution, not the detector.
#
# cpu_limit is a fraction of ONE core and only bites below what the service
# actually uses. 0.05 was measured as a no-op on catalogue (idles at ~0.17%
# of a core; p95 flat at 4.8ms). 0.002 took it to 160ms. front-end idles near
# 1.8%, so 0.005 is an estimate well under that; orders is unmeasured - if
# its p95 doesn't move, lower it rather than suspecting the detector.
DEFAULT_SUITE: list[ScenarioSpec] = [
    ScenarioSpec("bad_deploy_latency", "catalogue", 90, {"cpu_limit": 0.002}),
    ScenarioSpec("bad_deploy_latency", "front-end", 90, {"cpu_limit": 0.005}),
    ScenarioSpec("bad_deploy_latency", "orders", 90, {"cpu_limit": 0.005}),
    ScenarioSpec("service_crash", "payment", 60),
    ScenarioSpec("service_crash", "user", 60),
    ScenarioSpec("service_crash", "shipping", 60),
    # Holds a write lock on catalogue's `sock` table (issue #35). Holding 155
    # server connections did nothing: catalogue reuses two pooled connections
    # and never asks for a new one. Expect a cliff, not a ramp - catalogue
    # stalls outright while the lock is held.
    ScenarioSpec("db_pool_saturation", "catalogue", 90),
]

# The four classes added to the injector in issue #35.
#
# `service` is the detection ground truth in every case — the service whose
# metrics are expected to move — while `dependency` records where the fault
# was physically applied. For dependency_timeout and config_error those
# differ, which is the point: the anomaly should surface on `catalogue` or
# `front-end`, and naming the actual culprit is M3's job, not the detector's.
MISSING_CLASS_SCENARIOS: list[ScenarioSpec] = [
    # A paused catalogue-db accepts connections and never answers, so
    # catalogue's queries hang instead of failing fast. Latency first, errors
    # only once its own timeouts fire.
    ScenarioSpec("dependency_timeout", "catalogue", 90, {"dependency": "catalogue-db"}),
    # front-end resolving `carts` to loopback: the service stays up and
    # answers 500s, unlike a crash, so it exercises the error-rate path rather
    # than the staleness path. Not `catalogue`: a refused connection there
    # crashes front-end outright, so it crash-loops and reads as an outage
    # (measured in #35).
    ScenarioSpec("config_error", "front-end", 90, {"dependency": "carts"}),
    # carts is a JVM with a heap well above 64MB, so the limit lands below
    # its working set and the kernel OOM-kills it — a memory-caused outage
    # rather than an arbitrary stop.
    ScenarioSpec("memory_exhaustion", "carts", 90, {"limit_mb": 64}),
    # The only slow-drift fault in the set: 180s in 6 steps is 30s per step,
    # so the degradation is gradual enough that a cumulative statistic
    # (CUSUM) can plausibly beat a per-sample one (3-sigma). Every other
    # fault here is a step change, which is why the detectors have tied on
    # all of them so far (issue #34).
    ScenarioSpec("resource_exhaustion", "user", 180, {"cpu_limit": 0.002, "steps": 6}),
]

FULL_SUITE: list[ScenarioSpec] = DEFAULT_SUITE + MISSING_CLASS_SCENARIOS

# A quick pass for wiring checks, so nobody waits 20 minutes to find out the
# consumer was misconfigured.
SMOKE_SUITE: list[ScenarioSpec] = [
    ScenarioSpec("bad_deploy_latency", "catalogue", 60, {"cpu_limit": 0.002}),
    ScenarioSpec("service_crash", "payment", 45),
]

# One scenario per new fault class, for verifying the injector's new code
# lands a real effect before spending an hour on the full suite.
NEW_FAULTS_SUITE: list[ScenarioSpec] = list(MISSING_CLASS_SCENARIOS)

SUITES: dict[str, list[ScenarioSpec]] = {
    "default": DEFAULT_SUITE,
    "full": FULL_SUITE,
    "smoke": SMOKE_SUITE,
    "new-faults": NEW_FAULTS_SUITE,
}


# Fault magnitudes for the severity sweep (issue #34), weakest first. A higher
# cpu_limit is a gentler fault: the quota only bites below what the service
# actually uses. 0.05 was measured as a near no-op on catalogue and 0.002 took
# the same service past 600ms, so the range spans "invisible" to "unmissable"
# and the interesting answer is somewhere in between.
#
# These are a starting range, not a result. What the sweep reports is detection
# rate against *measured* impact, because a quota that cripples one service is
# a no-op on another and cpu_limit is not comparable across them.
SEVERITY_LEVELS: tuple[float, ...] = (0.05, 0.02, 0.008, 0.002)


def severity_suite(
    cpu_limit: float,
    services: tuple[str, ...] = ("catalogue",),
    duration_s: int = 90,
) -> list[ScenarioSpec]:
    """A `bad_deploy_latency` suite at one fault magnitude.

    One fault type on purpose. A sweep is only interpretable if severity is the
    only thing that changed between rungs, and `service_crash` has no magnitude
    to vary - a stopped container is stopped.
    """
    for service in services:
        if service not in KNOWN_SERVICES:
            raise ValueError(f"unknown service {service!r}")
    return [
        ScenarioSpec("bad_deploy_latency", service, duration_s, {"cpu_limit": cpu_limit})
        for service in services
    ]


def partition_by_support(
    specs: list[ScenarioSpec], supported: set[str] | None
) -> tuple[list[ScenarioSpec], list[ScenarioSpec]]:
    """Split specs into what this injector can produce and what it cannot.

    `supported is None` means the injector could not be asked, in which case
    every spec is attempted — a preflight that cannot reach the injector is
    not evidence that a fault type is missing.

    Skipping unsupported scenarios up front matters for the headline number:
    a scenario that never injected is not a scenario the detector missed, and
    counting it as one would understate detection by however many fault
    classes the injector happens to lack that week.
    """
    if supported is None:
        return list(specs), []
    runnable: list[ScenarioSpec] = []
    unsupported: list[ScenarioSpec] = []
    for spec in specs:
        (runnable if spec.fault_type in supported else unsupported).append(spec)
    return runnable, unsupported
