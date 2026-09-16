"""Shared test setup. pytest imports this before any test module imports app.main."""

import json
import os
from datetime import datetime, timezone

import pytest  # noqa: E402

from app.db import DatabaseUnavailable  # noqa: E402
from app.main import app, get_chat, get_embedder, get_store  # noqa: E402
from app.models import REUSABLE_ANSWERS, AnomalyEvent  # noqa: E402
from app.ollama import ChatReply  # noqa: E402
from app.retrieval import EMBEDDING_DIMENSIONS  # noqa: E402
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
        self.events = {event.anomaly_id: event for event in events}
        self.incidents: list = []  # what search_incidents returns; empty means no corpus
        self.saved: list = []  # (StoredAnalysis, evidence) pairs, oldest first
        self.fail_saves = False

    def get(self, anomaly_id: str) -> AnomalyEvent | None:
        return self.events.get(anomaly_id)

    def scoring_inputs(self, anomaly_id: str, window_seconds: float, lookback_minutes: float):
        event = self.events.get(anomaly_id)
        return None if event is None else ScoringInputs(anomaly=event, related=[], deploys=[])

    def incident_count(self) -> int:
        return len(self.incidents)

    def search_incidents(self, vector, services, fault_types, top_k, hybrid):
        return self.incidents[:top_k]

    def save_analysis(self, analysis, evidence):
        if self.fail_saves:
            raise DatabaseUnavailable("cannot reach TimescaleDB at timescaledb:5432")
        stored = analysis.model_copy(update={"created_at": datetime.now(timezone.utc)})
        self.saved.append((stored, list(evidence)))
        return stored.created_at

    def latest_reusable_analysis(self, anomaly_id, pipeline_mode, model_version, config_fingerprint):
        key = (anomaly_id, pipeline_mode, model_version, config_fingerprint)
        for analysis, _ in reversed(self.saved):
            identity = (analysis.anomaly_id, analysis.pipeline_mode, analysis.model_version, analysis.config_fingerprint)
            if identity == key and analysis.answered_by in REUSABLE_ANSWERS:
                return analysis
        return None

    def analyses(self, anomaly_id, pipeline_mode=None, limit=20):
        found = [a for a, _ in reversed(self.saved) if a.anomaly_id == anomaly_id and pipeline_mode in (None, a.pipeline_mode)]
        return found[:limit]

    def status(self) -> str:
        return "ok"


def fake_embed(texts: list[str]) -> list[list[float]]:
    return [[0.0] * EMBEDDING_DIMENSIONS for _ in texts]


def fake_chat(messages, schema) -> ChatReply:
    """A well-behaved LLM: one hypothesis about the first listed candidate, citing only the anomaly."""
    top = schema["properties"]["hypotheses"]["items"]["anyOf"][0]["properties"]
    content = {
        "hypotheses": [
            {
                "rank": 1,
                "service": top["service"]["enum"][0],
                "cause": "fake LLM cause",
                "confidence": 0.5,
                "evidence_ids": [top["evidence_ids"]["items"]["enum"][0]],
                "proposed_action": "no_action",
            }
        ]
    }
    return ChatReply(content=json.dumps(content), prompt_tokens=100, output_tokens=40)


@pytest.fixture(autouse=True)
def fake_store():
    """Endpoint tests run against an in-memory store holding anom-0001 only, and never call
    the real Ollama."""
    store = FakeAnomalyStore(STORED_ANOMALY)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_embedder] = lambda: fake_embed
    app.dependency_overrides[get_chat] = lambda: fake_chat
    yield store
    app.dependency_overrides.clear()
