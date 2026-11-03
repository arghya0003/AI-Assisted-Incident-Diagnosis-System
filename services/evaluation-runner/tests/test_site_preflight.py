import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sources import reporting_services  # noqa: E402


class FakeCursor:
    def __init__(self, reporting, log):
        self._reporting, self._log = reporting, log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params):
        self._log.append((query, params))
        self._asked = params[0]

    def fetchall(self):
        return [(s,) for s in self._asked if s in self._reporting]


class FakeConn:
    def __init__(self, reporting):
        self.reporting, self.log = reporting, []

    def cursor(self):
        return FakeCursor(self.reporting, self.log)


def test_services_that_are_not_reporting_are_the_difference():
    """The runner refuses a site suite when this set falls short of the services it touches."""
    asked = {"ci-web", "ci-runner", "ci-deployer"}
    assert asked - reporting_services(FakeConn({"ci-web", "ci-runner"}), asked) == {"ci-deployer"}


def test_a_site_that_is_down_reports_nothing_so_every_service_is_missing():
    asked = {"stream-web", "stream-storage"}
    assert asked - reporting_services(FakeConn(set()), asked) == asked


def test_it_asks_about_request_metrics_inside_a_recent_window_for_exactly_these_services():
    conn = FakeConn({"ci-web"})
    reporting_services(conn, {"ci-web", "ci-runner"}, within_seconds=90)
    query, params = conn.log[0]
    assert "request_rate" in query
    assert params == (["ci-runner", "ci-web"], 90)
