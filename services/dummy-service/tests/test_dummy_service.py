import sys
import threading
import time

import pytest
import requests
from prometheus_client import CollectorRegistry

import main
from main import Config, create_app, parse_downstream


class Reply:
    def __init__(self, status_code):
        self.status_code = status_code


def client(call=None, rng=lambda: 0.5, **settings):
    settings = {"name": "stream-web", "work_ms": 0, "latency_ms": 0, "jitter_ms": 0, **settings}
    registry = CollectorRegistry()
    app = create_app(Config(**settings), call=call or (lambda *a, **k: Reply(200)),
                     registry=registry, rng=rng)
    return app.test_client()


def test_downstream_hosts_become_urls_in_call_order():
    assert parse_downstream("a:8080, b:8080") == ("http://a:8080", "http://b:8080")


def test_no_downstream_is_an_empty_tuple():
    assert parse_downstream("") == ()


def test_config_reads_the_environment():
    config = Config.from_env({"SERVICE_NAME": "stream-storage", "ERROR_RATE": "0.1",
                              "DOWNSTREAM": "x:8080"})
    assert config.name == "stream-storage"
    assert config.error_rate == 0.1
    assert config.downstream == ("http://x:8080",)


def test_a_healthy_chain_answers_200():
    response = client(downstream=("http://next",)).get("/work")
    assert response.status_code == 200
    assert response.get_json()["service"] == "stream-web"


def test_a_dead_downstream_is_a_502_so_the_failure_cascades():
    def refused(*args, **kwargs):
        raise requests.ConnectionError()

    assert client(refused, downstream=("http://next",)).get("/work").status_code == 502


def test_a_hung_downstream_is_a_504():
    def hung(*args, **kwargs):
        raise requests.Timeout()

    assert client(hung, downstream=("http://next",)).get("/work").status_code == 504


def test_a_failing_downstream_is_passed_up_as_a_502():
    assert client(lambda *a, **k: Reply(503), downstream=("http://next",)).get("/work").status_code == 502


def test_the_configured_error_rate_produces_500s():
    assert client(rng=lambda: 0.05, error_rate=0.1).get("/work").status_code == 500


def test_metrics_use_the_names_and_label_the_bridge_queries():
    """metrics-bridge filters on status_code and reads these exact names."""
    test_client = client()
    test_client.get("/work")
    text = test_client.get("/metrics").get_data(as_text=True)
    assert 'request_duration_seconds_count{route="/work",status_code="200"} 1.0' in text
    assert "request_duration_seconds_bucket" in text


def test_a_failed_request_is_counted_under_its_5xx_status():
    test_client = client(rng=lambda: 0.05, error_rate=0.1)
    test_client.get("/work")
    text = test_client.get("/metrics").get_data(as_text=True)
    assert 'request_duration_seconds_count{route="/work",status_code="500"} 1.0' in text


def test_the_page_and_health_checks_are_not_counted_as_traffic():
    test_client = client()
    test_client.get("/healthz")
    assert "request_duration_seconds_count" not in test_client.get("/metrics").get_data(as_text=True)


@pytest.mark.skipif(sys.platform != "linux", reason="process metrics read /proc")
def test_process_metrics_are_on_the_default_registry():
    """cpu_rate and memory_bytes come from these; a private registry would lose them."""
    from prometheus_client import generate_latest
    assert b"process_cpu_seconds_total" in generate_latest()


def test_a_call_to_itself_fails_at_once_and_is_not_traffic():
    """config_error sends a service to loopback, which is its own port here."""
    test_client = client()
    response = test_client.get("/work", headers={main.CALLER_HEADER: "stream-web"})
    assert response.status_code == 503
    assert "request_duration_seconds_count" not in test_client.get("/metrics").get_data(as_text=True)


def test_a_call_from_another_service_is_served():
    response = client().get("/work", headers={main.CALLER_HEADER: "stream-playback"})
    assert response.status_code == 200


def test_downstream_calls_name_the_caller():
    seen = {}

    def record(url, timeout, headers):
        seen.update(headers)
        return Reply(200)

    client(record, downstream=("http://next",)).get("/work")
    assert seen == {main.CALLER_HEADER: "stream-web"}


def test_ballast_is_written_so_it_counts_as_working_set():
    ballast = main.allocate_ballast(1)
    assert len(ballast) == 1024 * 1024
    assert all(ballast[offset] == 1 for offset in range(0, len(ballast), 4096))


def test_no_ballast_by_default():
    assert Config.from_env({}).ballast_mb == 0
    assert Config.from_env({"BALLAST_MB": "64"}).ballast_mb == 64


def test_a_request_with_no_free_slot_fails_and_counts_its_wait():
    entered, release = threading.Event(), threading.Event()

    def stuck(*args, **kwargs):
        entered.set()
        release.wait(5)
        return Reply(200)

    registry = CollectorRegistry()
    app = create_app(Config(name="ci-runner", work_ms=0, latency_ms=0, jitter_ms=0, slots=1, timeout_s=0.05,
                            downstream=("http://next",)), call=stuck, registry=registry)
    holder = threading.Thread(target=lambda: app.test_client().get("/work"))
    holder.start()
    assert entered.wait(5)

    response = app.test_client().get("/work")
    release.set()
    holder.join(5)

    assert response.status_code == 503
    assert response.get_json()["elapsed_ms"] >= 50
    text = app.test_client().get("/metrics").get_data(as_text=True)
    assert 'request_duration_seconds_count{route="/work",status_code="503"} 1.0' in text


def test_a_slot_is_given_back_after_a_failure():
    def refused(*args, **kwargs):
        raise requests.ConnectionError()

    test_client = client(refused, slots=1, downstream=("http://next",), timeout_s=0.05)
    assert [test_client.get("/work").status_code for _ in range(2)] == [502, 502]


def test_each_site_can_serve_its_own_page():
    assert b"BuildBoard" in client(page="board.html").get("/").get_data()
    assert b"StreamBox" in client().get("/").get_data()


def test_burn_cpu_spins_for_about_the_requested_time():
    """It must spin, not sleep, or a CPU throttle has nothing to take away."""
    # Long enough to cross several ticks of process_time, which advances in
    # 15.6 ms steps on Windows and made a 20 ms spin read as zero CPU.
    wall, cpu = time.perf_counter(), time.process_time()
    main.burn_cpu(100)
    assert time.perf_counter() - wall >= 0.1
    assert time.process_time() - cpu >= 0.05
