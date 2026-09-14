"""Phase 2 integration: app.db and the consumer's message handling against real TimescaleDB.

Skipped unless TimescaleDB is reachable (localhost:5432 by default; set TEST_PG_HOST to
override) with 005_diagnosis.sql applied. Each test runs in one transaction that is rolled
back, so nothing is left in the shared dev database.
"""

import dataclasses
import json
import math
import os
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest

from app.consumer import handle_message
from app.corpus import IncidentRecord
from app.db import (
    DatabaseUnavailable,
    PostgresAnomalyStore,
    connect,
    count_incidents,
    delete_incidents_except,
    get_anomaly,
    get_scoring_inputs,
    latest_reusable_analysis,
    list_analyses,
    missing_tables,
    save_analysis,
    save_anomaly,
    search_incidents,
    upsert_incident,
)
from app.models import Evidence, StoredAnalysis, StoredHypothesis
from app.models import AnomalyEvent
from app.settings import settings

TEST_SETTINGS = dataclasses.replace(settings, pg_host=os.environ.get("TEST_PG_HOST", "localhost"))

EVENT = {
    "anomaly_id": "anom-test-phase2-0001",
    "services": ["catalogue"],
    "metrics": ["latency_p95_ms"],
    "severity": "high",
    "t_detected": "2026-09-13T08:08:13.234Z",
    "t_onset": "2026-09-13T08:08:13.221Z",
    "evidence_window": {"start": "2026-09-13T08:08:13.221Z", "end": "2026-09-13T08:08:13.221Z"},
}


@pytest.fixture
def cur():
    try:
        conn = connect(TEST_SETTINGS, connect_timeout=2)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"TimescaleDB not reachable: {exc}")
    try:
        with conn.cursor() as cursor:
            missing = missing_tables(cursor)
            if missing:
                pytest.skip(f"005_diagnosis.sql not applied; missing tables {missing}")
            yield cursor
    finally:
        conn.rollback()
        conn.close()


def _save(cur, raw):
    return save_anomaly(cur, AnomalyEvent.model_validate(raw), raw)


def test_save_then_get_round_trips(cur):
    assert _save(cur, EVENT) == "inserted"
    assert get_anomaly(cur, EVENT["anomaly_id"]) == AnomalyEvent.model_validate(EVENT)
    cur.execute(
        "SELECT source, services, window_start = window_end FROM anomalies WHERE anomaly_id = %s",
        (EVENT["anomaly_id"],),
    )
    assert cur.fetchone() == ("kafka", ["catalogue"], True)


def test_redelivery_is_a_duplicate(cur):
    _save(cur, EVENT)
    assert _save(cur, EVENT) == "duplicate"


def test_same_id_different_content_is_a_collision_and_keeps_the_first(cur):
    _save(cur, EVENT)
    assert _save(cur, {**EVENT, "services": ["user"]}) == "collision"
    assert get_anomaly(cur, EVENT["anomaly_id"]).services == ["catalogue"]


def test_raw_keeps_fields_m2_adds_later(cur):
    _save(cur, {**EVENT, "z_score": 4.2})
    cur.execute("SELECT raw->>'z_score' FROM anomalies WHERE anomaly_id = %s", (EVENT["anomaly_id"],))
    assert cur.fetchone() == ("4.2",)


def test_unknown_anomaly_is_none(cur):
    assert get_anomaly(cur, "anom-test-phase2-does-not-exist") is None


@pytest.mark.parametrize(
    "value",
    [None, b"not json", b"\xff\xfe", b"[1, 2]", json.dumps({**EVENT, "services": []}).encode()],
    ids=["tombstone", "not-json", "not-utf8", "not-an-object", "fails-validation"],
)
def test_consumer_skips_malformed_messages(cur, value):
    assert handle_message(cur, value) == "invalid"


def test_consumer_stores_a_valid_message(cur):
    assert handle_message(cur, json.dumps(EVENT).encode()) == "inserted"
    assert handle_message(cur, json.dumps(EVENT).encode()) == "duplicate"


def test_store_reports_ok_against_the_real_database(cur):
    store = PostgresAnomalyStore(TEST_SETTINGS)
    assert store.status() == "ok"
    assert store.get("anom-test-phase2-does-not-exist") is None


def _event_at(anomaly_id, onset, services=("catalogue",)):
    stamp = onset.isoformat()
    return {
        **EVENT,
        "anomaly_id": anomaly_id,
        "services": list(services),
        "t_detected": stamp,
        "t_onset": stamp,
        "evidence_window": {"start": stamp, "end": stamp},
    }


def test_scoring_inputs_apply_the_window_and_source(cur):
    # 2020, so no real anomaly or deploy falls in these windows.
    onset = datetime(2020, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    main = _event_at("anom-test-p4-main", onset)
    near = _event_at("anom-test-p4-near", onset + timedelta(seconds=60), ["user"])
    far = _event_at("anom-test-p4-far", onset + timedelta(seconds=300))
    fixture_source = _event_at("anom-test-p4-fixture", onset + timedelta(seconds=10))
    for raw in (main, near, far):
        _save(cur, raw)
    save_anomaly(cur, AnomalyEvent.model_validate(fixture_source), fixture_source, source="fixture")

    deploys = [
        ("dep-test-p4-in", onset - timedelta(minutes=5)),
        ("dep-test-p4-too-old", onset - timedelta(minutes=40)),
        ("dep-test-p4-after", onset + timedelta(minutes=1)),
    ]
    for deploy_id, time in deploys:
        cur.execute(
            "INSERT INTO deploys (deploy_id, service, version, commit_sha, config_diff, time) "
            "VALUES (%s, 'catalogue', '1.0.0', 'abc1234', NULL, %s)",
            (deploy_id, time),
        )

    inputs = get_scoring_inputs(cur, "anom-test-p4-main", window_seconds=120, lookback_minutes=30)
    assert inputs.anomaly.anomaly_id == "anom-test-p4-main"
    assert [a.anomaly_id for a in inputs.related] == ["anom-test-p4-near"]
    assert [d.deploy_id for d in inputs.deploys] == ["dep-test-p4-in"]
    assert inputs.deploys[0].config_diff is None


def test_scoring_inputs_for_an_unknown_anomaly_is_none(cur):
    assert get_scoring_inputs(cur, "anom-test-p4-missing", 120, 30) is None
    assert PostgresAnomalyStore(TEST_SETTINGS).scoring_inputs("anom-test-p4-missing", 120, 30) is None


def _unit_vector(index, other=None, weight=0.0):
    vector = [0.0] * 768
    vector[index] = 1.0
    if other is not None:
        vector[other] = weight
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector]


def _incident(incident_id, services, fault_type, title="test incident"):
    source = "synthetic" if services else "public_postmortem"
    return IncidentRecord(
        incident_id=incident_id,
        title=title,
        services=services,
        fault_type=fault_type,
        source=source,
        source_url=None if services else "https://example.com/postmortem",
        body="**Symptoms:** s\n\n**Root cause:** r\n\n**Resolution:** x",
    )


# Ids 99xx never collide with the real corpus (0001-0035, 0101-0126).
TEST_INCIDENTS = [
    (_incident("incident-9901", ["catalogue"], "db_pool_saturation"), _unit_vector(0)),  # similarity 1.0
    (_incident("incident-9902", ["orders"], "bad_deploy_latency"), _unit_vector(0, 1, 0.5)),  # 0.894
    (_incident("incident-9903", [], None), _unit_vector(0, 2, 0.2)),  # 0.981
]


def _test_ids(results):
    return [r.incident_id for r in results if r.incident_id.startswith("incident-99")]


def test_incident_search_vector_versus_hybrid(cur):
    for record, vector in TEST_INCIDENTS:
        upsert_incident(cur, record, vector)
    query = _unit_vector(0)

    vector_results = search_incidents(cur, query, ["catalogue"], ["bad_deploy_latency"], top_k=1000, hybrid=False)
    assert _test_ids(vector_results) == ["incident-9901", "incident-9903", "incident-9902"]
    best = next(r for r in vector_results if r.incident_id == "incident-9901")
    assert best.similarity == pytest.approx(1.0) and best.services == ["catalogue"]
    assert (best.root_cause, best.resolution) == ("r", "x")

    # 9901 names a candidate service, 9902 has a matching fault type, 9903 matches neither.
    hybrid_results = search_incidents(cur, query, ["catalogue"], ["bad_deploy_latency"], top_k=1000, hybrid=True)
    assert _test_ids(hybrid_results) == ["incident-9901", "incident-9902"]


def test_upsert_incident_is_idempotent(cur):
    record, vector = TEST_INCIDENTS[0]
    before = count_incidents(cur)
    upsert_incident(cur, record, vector)
    upsert_incident(cur, record.model_copy(update={"title": "renamed"}), vector)
    assert count_incidents(cur) == before + 1
    cur.execute("SELECT title FROM incidents WHERE incident_id = %s", (record.incident_id,))
    assert cur.fetchone() == ("renamed",)


def test_delete_incidents_except_removes_the_rest(cur):
    for record, vector in TEST_INCIDENTS:
        upsert_incident(cur, record, vector)
    delete_incidents_except(cur, ["incident-9901"])
    cur.execute("SELECT incident_id FROM incidents")
    assert cur.fetchall() == [("incident-9901",)]  # rolled back after the test


P8_ANOMALY = "anom-test-p8-0001"


def _run(analysis_id, mode="full", answered_by="llm", fingerprint="fp0000000001", hypotheses=None):
    if hypotheses is None:
        hypotheses = [
            StoredHypothesis(rank=1, service="catalogue", cause="catalogue deploy", confidence=0.8,
                             evidence_ids=[P8_ANOMALY, "dep-test-p8"], proposed_action="rollback_deploy:dep-test-p8"),
            StoredHypothesis(rank=2, service="front-end", cause="front-end symptom", confidence=0.2,
                             evidence_ids=[P8_ANOMALY], proposed_action="no_action"),
        ]
    return StoredAnalysis(
        analysis_id=analysis_id, anomaly_id=P8_ANOMALY, pipeline_mode=mode, answered_by=answered_by,
        model_version="none" if mode == "deterministic" else "phi4-mini", config_fingerprint=fingerprint,
        llm_attempts=0 if mode == "deterministic" else 1, guardrail_rejected=0, latency_ms=1234,
        fallback_reason=None if answered_by in ("llm", "deterministic") else "ollama unavailable",
        hypotheses=hypotheses,
    )


def _evidence(relevance=0.9):
    return Evidence(
        evidence_id=f"ev:{P8_ANOMALY}:deployment:dep-test-p8", incident_id=P8_ANOMALY, category="deployment",
        source_id="dep-test-p8", service="catalogue", observed_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        relevance=relevance, summary="catalogue deployed", payload={"version": "1.0.0"},
    )


def test_a_stored_run_reads_back_with_its_hypotheses_and_evidence(cur):
    created = save_analysis(cur, _run("an-test-p8-a"), [_evidence()])
    (run,) = list_analyses(cur, P8_ANOMALY)
    assert run.created_at == created
    assert [(h.rank, h.service) for h in run.hypotheses] == [(1, "catalogue"), (2, "front-end")]
    assert run.hypotheses[0].evidence_ids == [P8_ANOMALY, "dep-test-p8"]
    cur.execute("SELECT hypothesis_id FROM hypotheses WHERE analysis_id = 'an-test-p8-a' ORDER BY rank")
    assert cur.fetchall() == [("an-test-p8-a-1",), ("an-test-p8-a-2",)]
    cur.execute("SELECT relevance, payload FROM evidence WHERE incident_id = %s", (P8_ANOMALY,))
    assert cur.fetchall() == [(0.9, {"version": "1.0.0"})]


def test_evidence_is_updated_when_an_anomaly_is_analysed_again(cur):
    save_analysis(cur, _run("an-test-p8-a"), [_evidence(0.9)])
    save_analysis(cur, _run("an-test-p8-b"), [_evidence(0.4)])
    cur.execute("SELECT relevance FROM evidence WHERE incident_id = %s", (P8_ANOMALY,))
    assert cur.fetchall() == [(0.4,)]


def test_a_run_without_hypotheses_is_still_recorded(cur):
    save_analysis(cur, _run("an-test-p8-failed", mode="llm_only", answered_by="llm_failed", hypotheses=[]), [])
    (run,) = list_analyses(cur, P8_ANOMALY)
    assert run.answered_by == "llm_failed" and run.hypotheses == []


def test_the_cache_lookup_serves_only_the_newest_reusable_run_for_its_key(cur):
    save_analysis(cur, _run("an-test-p8-old"), [])
    save_analysis(cur, _run("an-test-p8-new"), [])
    save_analysis(cur, _run("an-test-p8-fallback", answered_by="deterministic_fallback"), [])  # newest, not reusable
    save_analysis(cur, _run("an-test-p8-otherfp", fingerprint="fp0000000002"), [])
    save_analysis(cur, _run("an-test-p8-det", mode="deterministic", answered_by="deterministic"), [])

    found = latest_reusable_analysis(cur, P8_ANOMALY, "full", "phi4-mini", "fp0000000001")
    assert found.analysis_id == "an-test-p8-new"
    assert latest_reusable_analysis(cur, P8_ANOMALY, "deterministic", "none", "fp0000000001").analysis_id == "an-test-p8-det"
    assert latest_reusable_analysis(cur, P8_ANOMALY, "no_graph", "phi4-mini", "fp0000000001") is None
    assert [r.analysis_id for r in list_analyses(cur, P8_ANOMALY, pipeline_mode="full")][:2] == [
        "an-test-p8-otherfp",
        "an-test-p8-fallback",
    ]


def test_store_reports_an_unreachable_database():
    store = PostgresAnomalyStore(dataclasses.replace(settings, pg_host="127.0.0.1", pg_port=1))
    assert store.status() == "unreachable"
    with pytest.raises(DatabaseUnavailable):
        store.get("anom-0001")
    with pytest.raises(DatabaseUnavailable):
        store.scoring_inputs("anom-0001", 120, 30)
