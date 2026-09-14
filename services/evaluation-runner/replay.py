"""
Offline replay — where the ablation numbers come from.

Every detector is run over the *same* recorded metric stream, pulled back out
of TimescaleDB, with the same grouping and deploy-window policy applied. That
is what makes "EWMA beat a static threshold by X" a claim about the
detectors rather than about which one happened to run on a quieter afternoon.

Replaying also makes the comparison deterministic and repeatable: the grouper
is driven by each sample's own timestamp rather than the wall clock, so a
given window of recorded data always produces exactly the same events.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

from scoring import DetectedEvent, to_detected_event


def _load_detector_package() -> None:
    """Make the anomaly-detector modules importable.

    The evaluation runner scores the detector it does not own, so it has to
    reach into that service's code. Both layouts are supported: the Docker
    image copies it to /app/detector, while a local checkout sits beside it
    in services/.
    """
    candidates = [
        os.environ.get("DETECTOR_PACKAGE_PATH"),
        "/app/detector",
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "anomaly-detector")),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(os.path.join(candidate, "detectors.py")):
            if candidate not in sys.path:
                sys.path.insert(0, candidate)
            return
    raise ImportError(
        "could not locate the anomaly-detector modules; set DETECTOR_PACKAGE_PATH"
    )


_load_detector_package()

from deploy_window import DeployWindowTracker  # noqa: E402
from detectors import build_detector  # noqa: E402
from grouping import AnomalyGrouper  # noqa: E402
from staleness import StalenessMonitor  # noqa: E402


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def replay(
    samples: list[tuple[datetime, str, str, float]],
    detector_kind: str,
    deploys: list[tuple[str, str, datetime]] | None = None,
    warmup: int = 10,
    required_breaches: int = 2,
    group_delay_seconds: float = 15.0,
    cooldown_seconds: float = 120.0,
    deploy_window_seconds: float = 120.0,
    stale_after_seconds: float = 30.0,
) -> list[DetectedEvent]:
    """Run one detector over a recorded stream and return the events it emits."""
    tracker = DeployWindowTracker(window_seconds=deploy_window_seconds)
    for deploy_id, service, at in deploys or []:
        tracker.record(deploy_id, service, at)

    grouper = AnomalyGrouper(
        group_delay_seconds=group_delay_seconds,
        cooldown_seconds=cooldown_seconds,
    )
    staleness = StalenessMonitor(stale_after_seconds=stale_after_seconds)
    detectors: dict[tuple[str, str], object] = {}
    events: list[DetectedEvent] = []
    last_time: datetime | None = None

    for sample_time, service, metric, value in samples:
        staleness.observe(service, sample_time)
        key = (service, metric)
        if key not in detectors:
            detectors[key] = build_detector(
                detector_kind, service=service, metric=metric,
                warmup=warmup, required_breaches=required_breaches,
            )

        needed, deploy_id = tracker.required_breaches(
            service, sample_time, required_breaches
        )
        signal = detectors[key].update(value, iso(sample_time), required_breaches=needed)
        if signal is not None:
            signal.in_deploy_window = deploy_id is not None
            signal.deploy_id = deploy_id
            grouper.add(signal)

        # Replay has no wall clock, so liveness is judged against the stream's
        # own time. A service that stops reporting is only noticed while some
        # other service is still reporting — if the entire pipeline stops,
        # the recording simply ends and there is nothing left to compare to.
        for signal in staleness.check(sample_time):
            grouper.add(signal)

        for payload in grouper.flush(sample_time):
            event = to_detected_event(payload)
            if event is not None:
                events.append(event)

        last_time = sample_time

    # Drain anything still buffered when the recording ends, or the final
    # incident of every run would be silently dropped.
    if last_time is not None:
        for payload in grouper.flush(last_time + timedelta(days=1)):
            event = to_detected_event(payload)
            if event is not None:
                events.append(event)

    return events
