"""Phase 5: the incident corpus, query construction, the retriever's control flow, and the Ollama
client. No database and no real Ollama; tests/test_db.py covers the pgvector search itself."""

import httpx
import pytest

from app.corpus import load_corpus, parse_incident
from app.graph import load_graph
from app.models import AnomalyEvent, SimilarIncident
from app.ollama import OllamaClient, OllamaUnavailable
from app.retrieval import (
    EMBEDDING_DIMENSIONS,
    Retriever,
    candidate_services,
    document_text,
    query_text,
    suspected_fault_types,
)

GRAPH = load_graph()
CORPUS = load_corpus()
SYNTHETIC = [r for r in CORPUS if r.source == "synthetic"]
PUBLIC = [r for r in CORPUS if r.source == "public_postmortem"]

ANOMALY = AnomalyEvent.model_validate(
    {
        "anomaly_id": "anom-t-1",
        "services": ["front-end"],
        "metrics": ["error_rate"],
        "severity": "high",
        "t_detected": "2026-09-14T11:00:18.472Z",
        "t_onset": "2026-09-14T11:00:18.466Z",
        "evidence_window": {"start": "2026-09-14T11:00:18.466Z", "end": "2026-09-14T11:00:18.466Z"},
    }
)


# ------------------------------------------------------------------ corpus


def test_corpus_size_is_within_the_plan():
    assert 50 <= len(CORPUS) <= 150


def test_corpus_ids_are_unique():
    ids = [r.incident_id for r in CORPUS]
    assert len(set(ids)) == len(ids)


def test_corpus_mixes_synthetic_and_public_incidents():
    assert len(SYNTHETIC) >= 30 and len(PUBLIC) >= 20


@pytest.mark.parametrize("record", SYNTHETIC, ids=[r.incident_id for r in SYNTHETIC])
def test_synthetic_incidents_name_real_services(record):
    assert set(record.services) <= GRAPH.nodes


def test_every_injectable_fault_type_has_several_incidents():
    for fault_type in ("bad_deploy_latency", "service_crash", "db_pool_saturation"):
        assert sum(r.fault_type == fault_type for r in SYNTHETIC) >= 3, fault_type


def test_public_postmortems_link_their_source():
    for record in PUBLIC:
        assert record.source_url.startswith("https://") or record.source_url.startswith("http://")
        assert record.source_url in record.body, f"{record.incident_id} body must cite its source"


GOOD_BODY = "**Symptoms:** s\n\n**Root cause:** r\n\n**Resolution:** x"


@pytest.mark.parametrize(
    "text, message",
    [
        ("no front matter at all", "front matter"),
        ("---\nincident_id: incident-0001\ntitle: t\nsource: synthetic\n---\n" + GOOD_BODY, "root-cause service"),
        ("---\nincident_id: incident-0001\ntitle: t\nsource: public_postmortem\n---\n" + GOOD_BODY, "source_url"),
        ("---\nincident_id: incident-0001\ntitle: t\nservices: [catalogue]\nsource: synthetic\n---\nno sections", "sections"),
        ("---\nincident_id: inc-1\ntitle: t\nservices: [catalogue]\nsource: synthetic\n---\n" + GOOD_BODY, "incident_id"),
        ("---\nincident_id: incident-0001\ntitle: t\nservices: [catalogue]\nsource: synthetic\nseverity: high\n---\n" + GOOD_BODY, "severity"),
    ],
)
def test_malformed_incident_files_are_rejected(text, message):
    with pytest.raises(ValueError, match=message):
        parse_incident(text)


def test_crlf_files_parse():
    text = "---\nincident_id: incident-0001\ntitle: t\nservices: [catalogue]\nsource: synthetic\n---\n" + GOOD_BODY
    assert parse_incident(text.replace("\n", "\r\n")).services == ["catalogue"]


# ------------------------------------------------------------------ query construction


def test_query_uses_the_nomic_query_prefix_and_readable_metrics():
    text = query_text(ANOMALY)
    assert text.startswith("search_query: ")
    assert "error rate" in text and "front-end" in text and "high" in text


def test_unknown_metrics_pass_through_unchanged():
    assert "queue_depth" in query_text(ANOMALY.model_copy(update={"metrics": ["queue_depth"]}))


def test_document_embeds_only_the_symptoms():
    record = CORPUS[0]
    text = document_text(record)
    assert text == "search_document: " + record.symptoms
    assert "Root cause" not in text and "Resolution" not in text and record.title not in text


def test_every_incident_has_symptoms_to_embed():
    for record in CORPUS:
        # Some public summaries say little about symptoms ("A cascading failure."), and entries
        # must not add facts the source doesn't state, so only require a few words.
        assert len(record.symptoms.split()) >= 3, record.incident_id
        assert "**" not in record.symptoms, f"{record.incident_id}: symptoms ran into the next section"


def test_suspected_fault_types():
    latency = suspected_fault_types(["latency_p95_ms"])
    assert {"bad_deploy_latency", "db_pool_saturation"} <= latency and "service_crash" not in latency
    assert "service_crash" in suspected_fault_types(["error_rate"])
    assert suspected_fault_types(["memory_bytes"]) == {"capacity", "benign"}
    assert suspected_fault_types(["queue_depth"]) == set()
    # A service that stopped reporting metrics at all: crashed, or cut off from what it needs.
    assert suspected_fault_types(["liveness"]) == {"service_crash", "dependency_failure"}


def test_candidate_services_match_scoring():
    assert candidate_services(ANOMALY, GRAPH) == {"front-end"} | set(GRAPH.downstream("front-end"))
    unknown = ANOMALY.model_copy(update={"services": ["checkout"]})
    assert candidate_services(unknown, GRAPH) == {"checkout"}


# ------------------------------------------------------------------ retriever


class FakeIndex:
    def __init__(self, count=5, results=()):
        self.count = count
        self.results = list(results)
        self.calls = []

    def incident_count(self):
        return self.count

    def search_incidents(self, vector, services, fault_types, top_k, hybrid):
        self.calls.append({"vector": vector, "services": services, "fault_types": fault_types, "top_k": top_k, "hybrid": hybrid})
        return self.results


def fake_embed(texts):
    return [[0.1] * EMBEDDING_DIMENSIONS for _ in texts]


PAST = SimilarIncident(
    incident_id="incident-0011",
    title="catalogue killed by out-of-memory",
    services=["catalogue"],
    fault_type="service_crash",
    source="synthetic",
    similarity=0.64,
)


def test_empty_corpus_skips_embedding():
    def must_not_embed(texts):
        raise AssertionError("embedded a query with nothing to search")

    result = Retriever(must_not_embed, FakeIndex(count=0), GRAPH).retrieve(ANOMALY)
    assert (result.status, result.incidents) == ("empty_corpus", [])


def test_unreachable_embedding_model_degrades_instead_of_failing():
    def down(texts):
        raise OllamaUnavailable("connection refused")

    result = Retriever(down, FakeIndex(), GRAPH).retrieve(ANOMALY)
    assert result.status == "embedding_unavailable" and "connection refused" in result.detail


def test_wrong_embedding_size_degrades_instead_of_failing():
    index = FakeIndex()
    result = Retriever(lambda texts: [[0.1] * 10], index, GRAPH).retrieve(ANOMALY)
    assert result.status == "embedding_unavailable"
    assert index.calls == []


@pytest.mark.parametrize("mode, hybrid", [("hybrid", True), ("vector", False)])
def test_search_receives_the_candidate_filter(mode, hybrid):
    index = FakeIndex(results=[PAST])
    result = Retriever(fake_embed, index, GRAPH, mode=mode, top_k=3).retrieve(ANOMALY)
    assert (result.status, result.incidents) == ("ok", [PAST])
    (call,) = index.calls
    assert call["hybrid"] is hybrid and call["top_k"] == 3
    assert call["services"] == sorted(candidate_services(ANOMALY, GRAPH))
    assert call["fault_types"] == sorted(suspected_fault_types(ANOMALY.metrics))


@pytest.mark.parametrize("kwargs", [{"mode": "bm25"}, {"top_k": 0}])
def test_invalid_retriever_settings_are_rejected(kwargs):
    with pytest.raises(ValueError):
        Retriever(fake_embed, FakeIndex(), GRAPH, **kwargs)


# ------------------------------------------------------------------ Ollama client


def make_client(responses, sleeps):
    """A client whose transport replays `responses`: status codes, or exceptions to raise."""
    calls = []

    def handler(request):
        calls.append(request)
        outcome = responses[min(len(calls), len(responses)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        if outcome == 200:
            return httpx.Response(200, json={"embeddings": [[1.0, 0.0]]})
        return httpx.Response(outcome, text="runner crashed")

    client = OllamaClient("http://ollama.test", transport=httpx.MockTransport(handler), sleep=sleeps.append)
    return client, calls


def test_embed_posts_the_model_and_inputs():
    sleeps = []
    client, calls = make_client([200], sleeps)
    assert client.embed(["hello"], "nomic-embed-text") == [[1.0, 0.0]]
    assert calls[0].url.path == "/api/embed"
    assert b'"model":"nomic-embed-text"' in calls[0].content.replace(b" ", b"")
    assert sleeps == []


def test_server_errors_are_retried():
    sleeps = []
    client, calls = make_client([500, 200], sleeps)
    assert client.embed(["hello"], "nomic-embed-text") == [[1.0, 0.0]]
    assert len(calls) == 2 and sleeps == [1.0]


def test_transport_errors_are_retried():
    sleeps = []
    client, calls = make_client([httpx.ConnectError("connection refused"), 200], sleeps)
    assert client.embed(["hello"], "nomic-embed-text") == [[1.0, 0.0]]
    assert len(calls) == 2


def test_gives_up_after_three_attempts():
    sleeps = []
    client, calls = make_client([500], sleeps)
    with pytest.raises(OllamaUnavailable, match="3 attempts"):
        client.embed(["hello"], "nomic-embed-text")
    assert len(calls) == 3 and sleeps == [1.0, 2.0]


@pytest.mark.parametrize(
    "body, match",
    [
        ({"text": "<html>proxy says hello</html>"}, "unparseable JSON"),
        ({"json": [1.0, 0.0]}, "returned list, expected a JSON object"),
    ],
    ids=["not json", "json but not an object"],
)
def test_an_unparseable_success_is_reported_as_unavailable(body, match):
    """A 2xx body that is not a JSON object is a protocol failure, not a Python error. Callers
    degrade on OllamaUnavailable only, so anything else would surface as a 500 from /analyze
    instead of the documented fallback."""
    client = OllamaClient(
        "http://ollama.test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, **body)),
        sleep=lambda seconds: None,
    )
    with pytest.raises(OllamaUnavailable, match=match):
        client.embed(["hello"], "nomic-embed-text")


def test_read_timeouts_are_not_retried():
    sleeps = []
    client, calls = make_client([httpx.ReadTimeout("timed out")], sleeps)
    with pytest.raises(OllamaUnavailable, match="timed out"):
        client.embed(["hello"], "nomic-embed-text")
    assert len(calls) == 1 and sleeps == []


def test_client_errors_are_not_retried():
    sleeps = []
    client, calls = make_client([404], sleeps)
    with pytest.raises(OllamaUnavailable, match="404"):
        client.embed(["hello"], "not-pulled")
    assert len(calls) == 1 and sleeps == []


def test_mismatched_embedding_count_is_rejected():
    sleeps = []
    client, _ = make_client([200], sleeps)
    with pytest.raises(OllamaUnavailable, match="1 embeddings for 2 inputs"):
        client.embed(["one", "two"], "nomic-embed-text")
