"""The health check and the site filter on /api/recent.

Upstream services are replaced with fakes, so these need no running stack.
"""
import time

import pytest
import requests

import main


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self.ok = status < 400
        self._body = body if body is not None else {}

    def json(self):
        return self._body


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(main, "query", lambda *a, **k: [{"?column?": 1}])
    return main.app.test_client()


def test_health_reports_each_backend(client, monkeypatch):
    def fake_get(url, timeout):
        if url.startswith(main.ORCHESTRATOR_URL):
            raise requests.ConnectionError("refused")
        if url.startswith(main.LOAD_GENERATOR_URL):
            return FakeResponse(body={"achieved_rps": 4.5})
        return FakeResponse()

    monkeypatch.setattr(main.requests, "get", fake_get)
    body = client.get("/api/health").get_json()

    assert body["checks"]["orchestrator"]["ok"] is False
    assert "refused" in body["checks"]["orchestrator"]["error"]
    for name in ("fault-injector", "diagnosis-service", "load-generator", "timescaledb"):
        assert body["checks"][name]["ok"] is True
    assert body["offered_rps"] == 4.5


def test_health_asks_the_load_generator_once(client, monkeypatch):
    calls = []

    def fake_get(url, timeout):
        calls.append(url)
        return FakeResponse(body={"offered_rps": 5.0})

    monkeypatch.setattr(main.requests, "get", fake_get)
    client.get("/api/health")

    assert sum(url.startswith(main.LOAD_GENERATOR_URL) for url in calls) == 1


def test_health_does_not_wait_for_backends_one_after_another(client, monkeypatch):
    def slow_get(url, timeout):
        time.sleep(0.3)
        return FakeResponse()

    monkeypatch.setattr(main.requests, "get", slow_get)
    started = time.monotonic()
    client.get("/api/health")

    # Four backends at 0.3 s each would take 1.2 s one after another.
    assert time.monotonic() - started < 0.9


def test_health_tolerates_a_non_json_load_generator(client, monkeypatch):
    class NotJson(FakeResponse):
        def json(self):
            raise ValueError("no json")

    monkeypatch.setattr(main.requests, "get", lambda url, timeout: NotJson())
    body = client.get("/api/health").get_json()

    assert body["offered_rps"] is None


def test_recent_passes_the_site_through(client, monkeypatch):
    seen = {}

    def fake_rows(hours, limit, site=None):
        seen.update(hours=hours, limit=limit, site=site)
        return []

    monkeypatch.setattr(main, "recent_rows", fake_rows)
    assert client.get("/api/recent?hours=6&limit=50&site=streaming").status_code == 200
    assert seen == {"hours": 6, "limit": 50, "site": "streaming"}


def test_recent_without_a_site_means_every_system(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(main, "recent_rows",
                        lambda hours, limit, site=None: seen.update(site=site) or [])
    client.get("/api/recent")
    assert seen["site"] is None


def test_recent_rejects_an_unknown_site(client, monkeypatch):
    monkeypatch.setattr(main, "recent_rows", lambda *a, **k: pytest.fail("should not query"))
    response = client.get("/api/recent?site=elsewhere")

    assert response.status_code == 400
    assert "site must be one of" in response.get_json()["error"]


def test_recent_rows_hands_the_site_to_the_query(monkeypatch):
    seen = {}
    monkeypatch.setattr(main, "query", lambda sql, params: seen.update(params) or [])
    main.recent_rows(24, 100, "cicd")

    assert seen["site"] == "cicd"
    assert seen["limit"] == 100
