"""Evidence resolution — PLAN.md's "Evidence must be inspectable" requirement.

`parse_evidence_id` is the part most likely to break silently, because M3 emits several id
shapes and one of them (`metrics`) has a colon-separated source id of its own.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app, get_store
from app.models import ResolvedEvidence, parse_evidence_id
from tests.conftest import FakeIncidentStore

client = TestClient(app)


@pytest.fixture(autouse=True)
def store():
    fake = FakeIncidentStore()
    app.dependency_overrides[get_store] = lambda: fake
    yield fake
    app.dependency_overrides.clear()


def test_bare_id_is_its_own_source():
    assert parse_evidence_id("dep-2026-09-22-0001") == (None, "dep-2026-09-22-0001")
    assert parse_evidence_id("anom-20260922T113050-b678bd-0001") == (None, "anom-20260922T113050-b678bd-0001")


def test_structured_id_splits_into_category_and_source():
    assert parse_evidence_id("ev:anom-1:deployment:dep-7") == ("deployment", "dep-7")
    assert parse_evidence_id("ev:anom-1:similar_incident:incident-0042") == ("similar_incident", "incident-0042")


def test_metrics_source_id_keeps_its_own_colons():
    """A metrics source_id is `service:metric:timestamp`, and the timestamp has colons too --
    splitting on every colon would truncate it."""
    category, source_id = parse_evidence_id("ev:anom-1:metrics:catalogue:latency_p99_ms:2026-08-12T20:45:00.123Z")
    assert category == "metrics"
    assert source_id == "catalogue:latency_p99_ms:2026-08-12T20:45:00.123Z"


def test_malformed_structured_id_falls_back_to_bare():
    assert parse_evidence_id("ev:incomplete") == (None, "ev:incomplete")


def test_dependency_evidence_resolves_without_a_database(store):
    response = client.get("/evidence/ev:anom-1:dependency:front-end->catalogue")
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "dependency"
    assert body["detail"] == {"from": "front-end", "to": "catalogue"}


def test_deploy_evidence_exposes_the_config_diff(store):
    store.evidence["dep-7"] = ResolvedEvidence(
        evidence_id="dep-7", kind="deploy", summary="catalogue 1.0.1 deployed",
        deploy={
            "deploy_id": "dep-7", "service": "catalogue", "version": "1.0.1",
            "commit_sha": "abc1234", "config_diff": "- timeout: 5s\n+ timeout: 30s",
            "time": "2026-09-22T11:30:00Z",
        },
    )
    body = client.get("/evidence/dep-7").json()
    assert body["kind"] == "deploy"
    # The deploy diff is the thing PLAN.md names explicitly, so assert it survives the round trip.
    assert "timeout: 30s" in body["deploy"]["config_diff"]


def test_past_incident_evidence_carries_the_postmortem(store):
    store.evidence["incident-0042"] = ResolvedEvidence(
        evidence_id="incident-0042", kind="past_incident", summary="Past incident: slow catalogue",
        past_incident={
            "incident_id": "incident-0042", "title": "slow catalogue",
            "body": "## Root cause\nN+1 image lookups.", "services": ["catalogue"],
            "fault_type": "bad_deploy_latency", "source": "generated",
        },
    )
    body = client.get("/evidence/incident-0042").json()
    assert body["kind"] == "past_incident"
    assert "N+1" in body["past_incident"]["body"]


def test_unresolvable_id_is_reported_not_errored(store):
    """The guardrail guarantees a cited id existed when the analysis ran, so a miss means the
    record aged out. The UI should say so, not show an error page."""
    response = client.get("/evidence/dep-long-gone")
    assert response.status_code == 200
    assert response.json()["kind"] == "unknown"


def test_database_outage_surfaces_as_503(store):
    store.unavailable = True
    assert client.get("/evidence/dep-7").status_code == 503
