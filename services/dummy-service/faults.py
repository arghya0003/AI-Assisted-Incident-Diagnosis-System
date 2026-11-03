"""
Application-level faults for a dummy service, set by the fault injector.

The infrastructure faults (crash, throttle, pause, memory cap) act on the
container from outside. These act on the service itself: a slow storage
backend, a flushed cache, a stuck CI runner, a flaky build step. That is the
vocabulary of the sites, and it is what separates a gradual queue from the step
change a CPU quota gives.

Every fault carries a time limit and clears itself when it passes. The injector
also clears it explicitly, but a fault left on by a crashed injector would poison
every later run, so the service does not rely on being told.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

MAX_EXTRA_LATENCY_MS = 10_000
MAX_TTL_S = 600


@dataclass(frozen=True)
class Fault:
    extra_latency_ms: float = 0.0
    error_rate: float = 0.0
    decay_s: float = 0.0  # 0 holds the latency; otherwise it fades to nothing over this long
    started: float = 0.0
    ttl_s: float = 0.0


def parse_fault(body: dict, now: float) -> Fault:
    """A request body -> a Fault, or ValueError with a message fit for a 400."""
    try:
        fault = Fault(
            extra_latency_ms=float(body.get("extra_latency_ms", 0)),
            error_rate=float(body.get("error_rate", 0)),
            decay_s=float(body.get("decay_s", 0)),
            started=now,
            ttl_s=float(body["ttl_s"]),
        )
    except KeyError:
        raise ValueError("ttl_s is required: a fault must say when it ends") from None
    except (TypeError, ValueError):
        raise ValueError("extra_latency_ms, error_rate, decay_s and ttl_s must be numbers") from None

    if not 0 <= fault.extra_latency_ms <= MAX_EXTRA_LATENCY_MS:
        raise ValueError(f"extra_latency_ms must be between 0 and {MAX_EXTRA_LATENCY_MS}")
    if not 0 <= fault.error_rate <= 1:
        raise ValueError("error_rate must be between 0 and 1")
    if fault.decay_s < 0:
        raise ValueError("decay_s must not be negative")
    if not 0 < fault.ttl_s <= MAX_TTL_S:
        raise ValueError(f"ttl_s must be between 0 and {MAX_TTL_S}")
    if fault.extra_latency_ms == 0 and fault.error_rate == 0:
        raise ValueError("a fault needs extra_latency_ms or error_rate")
    return fault


class FaultState:
    """The fault currently applied to one service, if any."""

    def __init__(self):
        self._lock = threading.Lock()
        self._fault: Fault | None = None

    def set(self, fault: Fault) -> None:
        with self._lock:
            self._fault = fault

    def clear(self) -> None:
        with self._lock:
            self._fault = None

    def current(self, now: float) -> tuple[float, float]:
        """(extra latency in ms, extra error rate) in force at `now`."""
        with self._lock:
            fault = self._fault
            if fault is None:
                return 0.0, 0.0
            age = now - fault.started
            if age >= fault.ttl_s:
                self._fault = None
                return 0.0, 0.0

        extra = fault.extra_latency_ms
        if fault.decay_s > 0:
            extra *= max(0.0, 1 - age / fault.decay_s)
        return extra, fault.error_rate

    def describe(self, now: float) -> dict:
        extra, errors = self.current(now)
        with self._lock:
            fault = self._fault
        if fault is None:
            return {"active": False}
        return {"active": True, "extra_latency_ms": round(extra, 1), "error_rate": errors,
                "ends_in_s": round(max(0.0, fault.ttl_s - (now - fault.started)), 1)}
