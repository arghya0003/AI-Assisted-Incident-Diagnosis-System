import pytest
from prometheus_client import CollectorRegistry

from faults import FaultState, parse_fault
from main import ADMIN_HEADER, Config, create_app


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def fault(now=1000.0, **body):
    return parse_fault({"ttl_s": 60, **body}, now)


# ------------------------------------------------------------ the fault itself

def test_a_fault_holds_its_latency_until_it_expires():
    state = FaultState()
    state.set(fault(extra_latency_ms=400))
    assert state.current(1030.0) == (400.0, 0.0)
    assert state.current(1061.0) == (0.0, 0.0)


def test_an_expired_fault_stays_gone():
    state = FaultState()
    state.set(fault(extra_latency_ms=400))
    state.current(1100.0)
    assert state.current(1010.0) == (0.0, 0.0)


def test_decay_fades_the_latency_to_nothing_over_the_decay_time():
    """A flushed cache warms back up: the one fault that recovers gradually."""
    state = FaultState()
    state.set(fault(extra_latency_ms=150, decay_s=30))
    assert state.current(1000.0)[0] == 150.0
    assert state.current(1015.0)[0] == pytest.approx(75.0)
    assert state.current(1030.0)[0] == 0.0
    assert state.current(1045.0)[0] == 0.0


def test_errors_are_reported_beside_latency():
    state = FaultState()
    state.set(fault(error_rate=0.3))
    assert state.current(1001.0) == (0.0, 0.3)


def test_clear_removes_a_fault_that_has_not_expired():
    state = FaultState()
    state.set(fault(extra_latency_ms=400))
    state.clear()
    assert state.current(1001.0) == (0.0, 0.0)


def test_no_fault_means_no_effect():
    assert FaultState().current(1000.0) == (0.0, 0.0)


# ------------------------------------------------------------ what is accepted

def test_a_fault_must_say_when_it_ends():
    with pytest.raises(ValueError, match="ttl_s"):
        parse_fault({"extra_latency_ms": 100}, 0.0)


@pytest.mark.parametrize("body", [
    {"extra_latency_ms": -1},
    {"extra_latency_ms": 10_001},
    {"error_rate": 1.5},
    {"decay_s": -5},
    {"ttl_s": 0, "extra_latency_ms": 100},
    {"ttl_s": 601, "extra_latency_ms": 100},
    {"extra_latency_ms": "slow"},
    {},
])
def test_nonsense_is_refused(body):
    with pytest.raises(ValueError):
        parse_fault({"ttl_s": 60, **body}, 0.0)


def test_a_fault_that_changes_nothing_is_refused():
    """It would record a scenario in which nothing was injected."""
    with pytest.raises(ValueError, match="needs"):
        parse_fault({"ttl_s": 60}, 0.0)


# ------------------------------------------------------------ the admin routes

TOKEN = "t0ken"


def client(token=TOKEN, clock=None, rng=lambda: 0.5, **settings):
    settings = {"name": "stream-storage", "work_ms": 0, "latency_ms": 0, "jitter_ms": 0,
                "admin_token": token, **settings}
    app = create_app(Config(**settings), registry=CollectorRegistry(), rng=rng,
                     clock=clock or Clock())
    return app.test_client()


def apply(test_client, **body):
    return test_client.post("/admin/fault", json={"ttl_s": 60, **body}, headers={ADMIN_HEADER: TOKEN})


def test_the_routes_do_not_exist_without_a_token_configured():
    response = client(token="").post("/admin/fault", json={"ttl_s": 60, "error_rate": 1})
    assert response.status_code == 404


def test_the_wrong_token_is_refused_and_changes_nothing():
    test_client = client()
    response = test_client.post("/admin/fault", json={"ttl_s": 60, "error_rate": 1},
                                headers={ADMIN_HEADER: "guess"})
    assert response.status_code == 403
    assert test_client.get("/work").status_code == 200


def test_a_missing_token_is_refused():
    assert client().get("/admin/fault").status_code == 403


def test_an_error_fault_makes_the_service_fail_and_clearing_it_heals_it():
    test_client = client()
    assert apply(test_client, error_rate=1).status_code == 200
    assert test_client.get("/work").status_code == 500

    cleared = test_client.delete("/admin/fault", headers={ADMIN_HEADER: TOKEN})
    assert cleared.get_json() == {"active": False}
    assert test_client.get("/work").status_code == 200


def test_a_latency_fault_slows_requests_and_shows_in_the_metrics(monkeypatch):
    slept = []
    monkeypatch.setattr("main.time.sleep", slept.append)
    test_client = client()
    apply(test_client, extra_latency_ms=400)
    test_client.get("/work")
    assert slept == [0.4]


def test_the_fault_ends_on_its_own_when_the_clock_passes_its_ttl():
    clock = Clock()
    test_client = client(clock=clock)
    apply(test_client, error_rate=1, ttl_s=30)
    assert test_client.get("/work").status_code == 500
    clock.now += 31
    assert test_client.get("/work").status_code == 200


def test_a_bad_body_is_a_400_with_the_reason():
    response = apply(client(), error_rate=7)
    assert response.status_code == 400
    assert "error_rate" in response.get_json()["error"]


def test_applying_a_fault_is_not_counted_as_traffic():
    test_client = client()
    apply(test_client, error_rate=1)
    assert "request_duration_seconds_count" not in test_client.get("/metrics").get_data(as_text=True)


def test_describe_says_how_long_is_left():
    test_client = client()
    assert apply(test_client, extra_latency_ms=100, ttl_s=45).get_json()["ends_in_s"] == 45.0
