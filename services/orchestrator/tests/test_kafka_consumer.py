"""The consumer thread's failure modes.

These exist because the thread died twice on cold starts and nothing noticed:
the HTTP API stayed healthy, `anomalies.detected` went unread for hours, and the
only symptom was incidents never being opened. Each test below pins one half of
that - it reconnects instead of dying, and it says so when it does die.
"""

import threading

import pytest
from kafka.errors import KafkaError, KafkaTimeoutError, NodeNotReadyError

from app import kafka_consumer
from app.kafka_consumer import ConsumerStatus


@pytest.fixture(autouse=True)
def fresh_status(monkeypatch):
    status = ConsumerStatus()
    monkeypatch.setattr(kafka_consumer, "STATUS", status)
    return status


class FakeConsumer:
    def __init__(self, messages=(), raise_on_iter=None):
        self._messages = list(messages)
        self._raise = raise_on_iter
        self.committed = 0
        self.closed = False

    def __iter__(self):
        if self._raise is not None:
            raise self._raise
        return iter(self._messages)

    def commit(self):
        self.committed += 1

    def close(self):
        self.closed = True


def settings():
    class S:
        kafka_bootstrap = "kafka:9092"
    return S()


# ---------------------------------------------------------------- connecting

def test_connect_retries_a_bootstrap_timeout(monkeypatch, fresh_status):
    """The exact failure that killed this thread in production.

    KafkaTimeoutError descends from RetriableError, not KafkaConnectionError, so
    catching only connection errors let it escape and end the thread.
    """
    attempts = []

    def flaky(*args, **kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise KafkaTimeoutError("Unable to bootstrap from kafka:9092")
        return FakeConsumer()

    monkeypatch.setattr(kafka_consumer, "KafkaConsumer", flaky)
    stop = threading.Event()
    consumer = kafka_consumer._connect(settings(), stop)

    assert consumer is not None, "a bootstrap timeout must be retried, not fatal"
    assert len(attempts) == 3
    assert fresh_status.connected is True


def test_connect_retries_any_kafka_error(monkeypatch, fresh_status):
    """Not a list of blessed exceptions - the next variant would escape again."""
    attempts = []

    def flaky(*args, **kwargs):
        attempts.append(1)
        if len(attempts) < 2:
            raise NodeNotReadyError("broker still starting")
        return FakeConsumer()

    monkeypatch.setattr(kafka_consumer, "KafkaConsumer", flaky)
    assert kafka_consumer._connect(settings(), threading.Event()) is not None


def test_connect_gives_up_when_asked_to_stop(monkeypatch, fresh_status):
    stop = threading.Event()

    def always_fails(*args, **kwargs):
        stop.set()
        raise KafkaTimeoutError("nope")

    monkeypatch.setattr(kafka_consumer, "KafkaConsumer", always_fails)
    assert kafka_consumer._connect(settings(), stop) is None


def test_a_failed_connection_is_recorded_not_swallowed(monkeypatch, fresh_status):
    stop = threading.Event()

    def always_fails(*args, **kwargs):
        stop.set()
        raise KafkaTimeoutError("Unable to bootstrap from kafka:9092")

    monkeypatch.setattr(kafka_consumer, "KafkaConsumer", always_fails)
    kafka_consumer._connect(settings(), stop)
    assert "KafkaTimeoutError" in fresh_status.last_error


# ------------------------------------------------------------------ running

def test_a_broker_that_drops_mid_run_is_reconnected(monkeypatch, fresh_status):
    """A Kafka restart should cost a gap, not the whole decision path."""
    made = []

    def consumers(*args, **kwargs):
        if not made:
            made.append("first")
            return FakeConsumer(raise_on_iter=KafkaError("broker went away"))
        made.append("second")
        return FakeConsumer()

    monkeypatch.setattr(kafka_consumer, "KafkaConsumer", consumers)
    monkeypatch.setattr(kafka_consumer, "_consume", _consume_that_stops_after(made))

    stop = threading.Event()
    kafka_consumer.run(object(), settings(), stop)
    assert len(made) >= 2, "the thread must build a second consumer, not exit"


def _consume_that_stops_after(made):
    def _consume(consumer, orchestrator, stop):
        if len(made) == 1:
            raise KafkaError("broker went away")
        stop.set()
    return _consume


def test_the_thread_says_so_when_it_exits(monkeypatch, fresh_status, caplog):
    """The silent part of the bug: everything else stayed healthy."""
    monkeypatch.setattr(kafka_consumer, "KafkaConsumer", lambda *a, **k: FakeConsumer())
    monkeypatch.setattr(kafka_consumer, "_consume",
                        lambda consumer, orchestrator, stop: stop.set())

    with caplog.at_level("WARNING"):
        kafka_consumer.run(object(), settings(), threading.Event())

    assert any("no longer being consumed" in r.message for r in caplog.records)
    assert fresh_status.exited is True


# ------------------------------------------------------------------- status

def test_status_distinguishes_connecting_from_exited():
    status = ConsumerStatus()
    assert status.as_dict()["state"] == "connecting"
    status.connected = True
    assert status.as_dict()["state"] == "consuming"
    status.exited = True
    assert status.as_dict()["state"] == "exited", (
        "exited must win over connected, or a dead thread reads as healthy"
    )


def test_status_carries_the_last_error_for_a_human():
    status = ConsumerStatus()
    assert "last_error" not in status.as_dict()
    status.last_error = "KafkaTimeoutError: Unable to bootstrap from kafka:9092"
    assert status.as_dict()["last_error"].startswith("KafkaTimeoutError")
