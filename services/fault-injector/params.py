"""
Request validation for POST /faults, kept apart from main.py so it can be
tested without a Docker socket or a database.

`build_params` turns a request body into the params a fault runs with - and
that `fault_scenarios.params` records - or raises ValueError with a message
fit to return as a 400.
"""

from __future__ import annotations

MAX_DURATION_SECONDS = 300  # safety cap - a forgotten fault can't run forever

KNOWN_SERVICES = ["front-end", "catalogue", "payment", "user", "carts", "orders", "shipping"]

FAULT_TYPES = {
    "bad_deploy_latency",
    "service_crash",
    "db_pool_saturation",
    # Issue #35: the other four classes the plan names. Each is physically
    # enforced on a real container, not a simulated metric.
    "dependency_timeout",
    "config_error",
    "memory_exhaustion",
    "resource_exhaustion",
}

# The default target for dependency_timeout and config_error: one edge per
# service from the dependency graph in CONTRACTS.md (read out of each image's
# own config, not guessed). front-end calls several services; carts is the
# default because front-end survives losing it and answers 500s, whereas a
# refused connection to `catalogue` crashes front-end outright (measured: it
# crash-looped through a 40s config_error), which turns the fault into a crash.
DEPENDENCIES = {
    "catalogue": "catalogue-db",
    "carts": "carts-db",
    "orders": "orders-db",
    "user": "user-db",
    "front-end": "carts",
    "shipping": "rabbitmq",
    "payment": None,
}

# Everything a dependency fault may be applied to: the testbed and nothing
# else. Pausing timescaledb, kafka or this injector itself would break the
# measurement rather than the system under test.
DEPENDENCY_TARGETS = frozenset(KNOWN_SERVICES) | {
    "catalogue-db", "carts-db", "orders-db", "user-db", "rabbitmq", "queue-master",
}

# A memory limit below this would kill a JVM before it finished starting,
# leaving the container in a restart loop after the fault ended.
MIN_MEMORY_LIMIT_MB = 48


def build_params(fault_type: str | None, service: str | None, body: dict) -> dict:
    if fault_type not in FAULT_TYPES:
        raise ValueError(f"fault_type must be one of {sorted(FAULT_TYPES)}")
    if service not in KNOWN_SERVICES:
        raise ValueError(f"service must be one of {KNOWN_SERVICES}")

    params: dict = {"duration_s": min(int(body.get("duration_s", 30)), MAX_DURATION_SECONDS)}

    if fault_type == "bad_deploy_latency":
        # cpu_limit is a fraction of ONE core, and a quota only bites when it
        # is below what the service actually uses. These are small Go/Node
        # services: under 5 req/s of standing load, catalogue idles at ~0.17%
        # of a core, so the old 0.05 (5%) left it ~30x more CPU than it
        # needed and the "fault" changed nothing - measured, p95 flat at
        # 4.8ms through a 90s throttle. 0.002 (0.2%) took the same service
        # from 4.8ms to 160ms p95 within 30s at unchanged request rate.
        # Raise it for a heavier service (front-end idles near 1.8%).
        params["cpu_limit"] = float(body.get("cpu_limit", 0.002))

    elif fault_type == "db_pool_saturation":
        # The fault takes a table lock rather than holding server connections
        # (see run_db_pool_saturation), so there is no `connections` knob any
        # more; one sent by an older client is ignored, not rejected.
        if service != "catalogue":
            raise ValueError("db_pool_saturation only supports service=catalogue (MySQL)")
        params["mechanism"] = "table_lock"

    elif fault_type in ("dependency_timeout", "config_error"):
        dependency = body.get("dependency") or DEPENDENCIES.get(service)
        if not dependency:
            raise ValueError(f"{service} has no known dependency; pass `dependency` explicitly")
        if dependency not in DEPENDENCY_TARGETS:
            raise ValueError(f"dependency must be one of {sorted(DEPENDENCY_TARGETS)}")
        if dependency == service:
            raise ValueError("dependency must be a different service from `service`")
        params["dependency"] = dependency

    elif fault_type == "memory_exhaustion":
        params["limit_mb"] = max(int(body.get("limit_mb", 64)), MIN_MEMORY_LIMIT_MB)

    elif fault_type == "resource_exhaustion":
        params["cpu_limit"] = float(body.get("cpu_limit", 0.002))
        params["start_cpu_limit"] = float(body.get("start_cpu_limit", 0.05))
        params["steps"] = max(int(body.get("steps", 6)), 1)
        if params["start_cpu_limit"] < params["cpu_limit"]:
            raise ValueError("start_cpu_limit must be at least cpu_limit - the ramp only tightens")

    return params


def ramp(start: float, end: float, steps: int) -> list[float]:
    """CPU limits for resource_exhaustion, one per step, ending at `end`.

    Geometric, not linear. Latency under a quota grows roughly with
    usage/quota, so a linear walk from 0.05 to 0.002 spends its first four
    steps above what the service even uses and lands the whole effect in the
    last one or two - a step change again, which is exactly what this fault
    exists not to be. Equal ratios between steps spread the degradation
    across the whole duration.
    """
    if steps <= 1 or start <= end:
        return [end]
    ratio = (end / start) ** (1 / steps)
    return [start * ratio ** (i + 1) for i in range(steps - 1)] + [end]
