"""Shared test setup. pytest imports this before any test module imports app.main."""

import os

# Unit tests must never start a real Kafka consumer.
os.environ.setdefault("CONSUMER_ENABLED", "false")

import pytest  # noqa: E402

from app.main import app, get_store  # noqa: E402
from app.models import AnomalyEvent  # noqa: E402
from app.scoring import ScoringInputs  # noqa: E402

STORED_ANOMALY = AnomalyEvent.model_validate(
    {
        "anomaly_id": "anom-0001",
        "services": ["catalogue", "front-end"],
        "metrics": ["latency_p99_ms", "error_rate"],
        "severity": "high",
        "t_detected": "2026-08-12T20:45:00.123Z",
        "t_onset": "2026-08-12T20:44:30.000Z",
        "evidence_window": {"start": "2026-08-12T20:40:00.000Z", "end": "2026-08-12T20:45:00.000Z"},
    }
)


class FakeAnomalyStore:
    def __init__(self, *events: AnomalyEvent):
        self._events = {event.anomaly_id: event for event in events}

    def get(self, anomaly_id: str) -> AnomalyEvent | None:
        return self._events.get(anomaly_id)

    def scoring_inputs(self, anomaly_id: str, window_seconds: float, lookback_minutes: float):
        event = self._events.get(anomaly_id)
        return None if event is None else ScoringInputs(anomaly=event, related=[], deploys=[])

    def status(self) -> str:
        return "ok"


@pytest.fixture(autouse=True)
def fake_store():
    """Endpoint tests run against an in-memory store holding anom-0001 only."""
    store = FakeAnomalyStore(STORED_ANOMALY)
    app.dependency_overrides[get_store] = lambda: store
    yield store
    app.dependency_overrides.clear()
