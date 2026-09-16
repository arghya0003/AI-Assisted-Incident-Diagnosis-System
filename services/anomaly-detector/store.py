"""
Durable record of every emitted anomaly — the `anomalies` table.

`anomalies.detected` is the live contract M4 consumes, but a Kafka topic is a
stream with 24h retention: nothing can look an anomaly up by its ID once it
has scrolled past. M3's `POST /analyze {anomaly_id}` needs exactly that
lookup, so every event is also written to TimescaleDB
(timescaledb/init/005_anomalies.sql), under the same `anomaly_id`.

Kafka stays the path that matters. A database outage must never stop an
alert reaching M4, so a failed write is logged and dropped rather than
raised, and the next write simply tries to reconnect.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger("anomaly-detector")

# ON CONFLICT makes a re-sent event harmless rather than a crash.
INSERT_SQL = """
INSERT INTO anomalies (
    anomaly_id, detector, services, metrics, severity,
    t_onset, t_detected, evidence_window_start, evidence_window_end, raw, source
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (anomaly_id) DO NOTHING
"""


def to_row(event: dict, source: str = "kafka") -> tuple:
    """Map an `anomalies.detected` payload onto the table's columns.

    The frozen contract fields get columns so ordinary queries need no JSON,
    and `raw` keeps the event verbatim: a consumer can rebuild exactly what
    was published, and a field added to the event later is stored without a
    schema change. `source` marks hand-written fixtures, which evaluation
    excludes; everything this writes came off the wire.
    """
    window = event["evidence_window"]
    return (
        event["anomaly_id"],
        event.get("detector", "unknown"),
        list(event["services"]),
        list(event["metrics"]),
        event["severity"],
        event["t_onset"],
        event["t_detected"],
        window["start"],
        window["end"],
        json.dumps(event),
        source,
    )


class AnomalyStore:
    """Writes events to the `anomalies` table, never letting a failure escape.

    `connect` is a zero-argument callable returning an autocommit DB-API
    connection. It is called lazily and again after any failure, so the
    detector can start before the database and ride out a restart of it.
    """

    def __init__(self, connect):
        self._connect = connect
        self._conn = None

    def save(self, event: dict) -> bool:
        """Persist one event. Returns whether it was written."""
        try:
            if self._conn is None or self._conn.closed:
                self._conn = self._connect()
            with self._conn.cursor() as cur:
                cur.execute(INSERT_SQL, to_row(event))
            return True
        except Exception as exc:
            log.warning(
                "could not persist %s to the anomalies table (%s); it was still "
                "published to Kafka. If the table is missing, apply "
                "timescaledb/init/005_anomalies.sql.",
                event.get("anomaly_id"), exc,
            )
            self._reset()
            return False

    def _reset(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None
