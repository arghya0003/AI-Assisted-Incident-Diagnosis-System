# Phase 4 Notes: TimescaleDB Sink

## Schema
`timescaledb/init/001_schema.sql` — runs automatically on first container startup
(standard Postgres `/docker-entrypoint-initdb.d/` behavior):

- `metrics` table → hypertable, partitioned by `time`. Mirrors CONTRACTS.md's
  `{service, metric, value, timestamp, labels{}}` shape directly (labels stored as JSONB).
- Index on `(service, metric, time DESC)` — the access pattern M2/M3 will actually use
  ("give me this service's this metric over time").
- Retention: raw samples dropped after 24h (matches the Kafka topic's own retention),
  rollups (below) kept 7 days.
- `metrics_1m` continuous aggregate — 1-minute buckets of avg/min/max/count per
  `{service, metric}`, refreshed every minute. This is what queries beyond "the last few
  minutes" should hit, not the raw hypertable.

## Consumer: metrics-sink
`services/metrics-sink/` — Kafka consumer group `metrics-sink`, reads `metrics.raw`,
batches up to 200 records (or whatever arrived within one ~1s poll window) into a single
`execute_values` INSERT, commits the DB transaction, then commits Kafka offsets. At-least-once:
a crash between DB commit and offset commit re-writes a few rows, not a problem for a
metrics stream.

Same `kafka-python-ng` fix as `metrics-bridge` (Phase 3) — `kafka-python` proper doesn't
work on Python 3.12.

## Proof it works
Brought the consumer up against the backlog `metrics-bridge` had already produced (~170k
messages accumulated since Phase 3) — drained it in about a minute, then settled into
real-time trickle. Measured end-to-end lag directly in SQL:

```sql
SELECT count(*) AS total_rows, max(time) AS latest_sample, now() - max(time) AS lag FROM metrics;
--  total_rows |       latest_sample        |       lag
-- ------------+----------------------------+-----------------
--      169751 | 2026-08-19 12:18:14.011+00 | 00:00:04.353788
```

~4.3-4.8s lag, steady (not growing) — under the plan's 5s target. Worth noting honestly:
this lag is dominated by the pipeline's own cadence (Prometheus scrapes every 5s,
`metrics-bridge` polls every 5s), not by TimescaleDB write latency, which is near-instant.
Tightening it further means shortening those upstream intervals, not touching this layer.

Also manually triggered the continuous aggregate (`CALL refresh_continuous_aggregate(...)`)
and confirmed real rolled-up data:

```
bucket                  | service   | metric         | avg    | sample_count
2026-08-19 12:18:00+00  | catalogue | latency_p95_ms | 8.579  | 5
```

## What this unblocks
M2 (anomaly detection) and M3 (RAG reasoning) now have a real, queryable time-series store
to read from instead of needing to talk to Kafka or Prometheus directly — `SELECT ... FROM
metrics WHERE service = 'catalogue' AND metric = 'latency_p95_ms' ORDER BY time DESC` works
today, against real data.
