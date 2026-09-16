import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from detectors import Signal  # noqa: E402
from grouping import AnomalyGrouper, format_ts  # noqa: E402
from store import INSERT_SQL, AnomalyStore, to_row  # noqa: E402

BASE = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)


def a_real_event() -> dict:
    """An event built by the real grouper, so the mapping tracks the contract."""
    grouper = AnomalyGrouper(group_delay_seconds=15)
    grouper.add(Signal(
        service="catalogue", metric="latency_p95_ms", value=362.5, baseline=5.7,
        score=18.6, severity="high", timestamp=format_ts(BASE), detector="ewma",
        onset_timestamp=format_ts(BASE - timedelta(seconds=5)),
        in_deploy_window=True, deploy_id="dep-2026-09-14-0365",
    ))
    return grouper.flush(BASE + timedelta(seconds=20))[0]


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        if self.conn.fail_execute:
            raise RuntimeError('relation "anomalies" does not exist')
        self.conn.executed.append((sql, params))


class FakeConnection:
    def __init__(self, fail_execute=False):
        self.fail_execute = fail_execute
        self.executed = []
        self.closed = 0

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = 1


def test_event_fields_map_onto_the_table_columns():
    event = a_real_event()

    row = to_row(event)

    assert row[0] == event["anomaly_id"]
    assert row[1] == "ewma"
    assert row[2] == ["catalogue"]
    assert row[3] == ["latency_p95_ms"]
    assert row[4] == "high"
    assert (row[5], row[6]) == (event["t_onset"], event["t_detected"])
    assert (row[7], row[8]) == (event["evidence_window"]["start"], event["evidence_window"]["end"])


def test_the_whole_event_is_kept_verbatim_in_raw():
    """A consumer must be able to rebuild exactly what was published.

    Storing only the fields without their own column would drop anything M2
    adds to the event later, silently.
    """
    event = a_real_event()

    raw = json.loads(to_row(event)[9])

    assert raw == event
    assert raw["in_deploy_window"] is True
    assert raw["related_deploy_ids"] == ["dep-2026-09-14-0365"]
    assert raw["contributors"][0]["value"] == 362.5


def test_events_off_the_wire_are_marked_as_such():
    """`source` keeps M3's hand-written fixtures out of evaluation."""
    assert to_row(a_real_event())[10] == "kafka"
    assert to_row(a_real_event(), source="fixture")[10] == "fixture"


def test_save_writes_one_row():
    conn = FakeConnection()
    store = AnomalyStore(lambda: conn)

    assert store.save(a_real_event()) is True
    assert len(conn.executed) == 1
    assert conn.executed[0][0] == INSERT_SQL


def test_a_resent_event_is_ignored_rather_than_failing():
    assert "ON CONFLICT (anomaly_id) DO NOTHING" in INSERT_SQL


def test_database_down_does_not_raise():
    """Alerts must keep reaching M4 through Kafka when the database is down."""
    def unreachable():
        raise ConnectionError("timescaledb: connection refused")

    assert AnomalyStore(unreachable).save(a_real_event()) is False


def test_a_failed_write_reconnects_on_the_next_event():
    broken, healthy = FakeConnection(fail_execute=True), FakeConnection()
    connections = iter([broken, healthy])
    store = AnomalyStore(lambda: next(connections))

    assert store.save(a_real_event()) is False
    assert broken.closed

    assert store.save(a_real_event()) is True
    assert len(healthy.executed) == 1
