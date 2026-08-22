-- Runs once, automatically, on first container startup (standard Postgres
-- image behavior for anything mounted into /docker-entrypoint-initdb.d/).

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Raw metric samples, one row per {service, metric, timestamp} sample
-- consumed off the metrics.raw Kafka topic. Matches CONTRACTS.md's
-- {service, metric, value, timestamp, labels{}} shape.
CREATE TABLE metrics (
    time    TIMESTAMPTZ      NOT NULL,
    service TEXT             NOT NULL,
    metric  TEXT             NOT NULL,
    value   DOUBLE PRECISION NOT NULL,
    labels  JSONB
);

SELECT create_hypertable('metrics', by_range('time'));

CREATE INDEX ON metrics (service, metric, time DESC);

-- Raw samples are only useful short-term (matches the 24h retention
-- already set on the metrics.raw Kafka topic itself) - the continuous
-- aggregate below is the long-lived, queryable history.
SELECT add_retention_policy('metrics', drop_after => INTERVAL '24 hours');

-- 1-minute rollup per {service, metric} - what M2/M3 should actually
-- query against for anything beyond "the last few minutes".
CREATE MATERIALIZED VIEW metrics_1m
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 minute', time) AS bucket,
    service,
    metric,
    avg(value)   AS avg_value,
    min(value)   AS min_value,
    max(value)   AS max_value,
    count(*)     AS sample_count
FROM metrics
GROUP BY bucket, service, metric
WITH NO DATA;

SELECT add_continuous_aggregate_policy('metrics_1m',
    start_offset      => INTERVAL '1 hour',
    end_offset        => INTERVAL '1 minute',
    schedule_interval  => INTERVAL '1 minute');

-- Keep rollups far longer than raw samples - this is the "downsampled
-- after" half of the open question in CONTRACTS.md.
SELECT add_retention_policy('metrics_1m', drop_after => INTERVAL '7 days');
