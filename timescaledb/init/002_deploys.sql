-- Deploy log. Lives in the same Postgres/TimescaleDB instance as the
-- metrics hypertable (the architecture diagram groups "TimescaleDB
-- hypertables, continuous aggregates, PostgreSQL deploy log" under one
-- Time-Series Store box) - no separate Postgres container needed.
--
-- No hypertable here: deploy volume is orders of magnitude lower than
-- metric samples, a plain indexed table is enough.

CREATE SEQUENCE IF NOT EXISTS deploy_id_seq;

CREATE TABLE IF NOT EXISTS deploys (
    deploy_id   TEXT PRIMARY KEY,
    service     TEXT NOT NULL,
    version     TEXT NOT NULL,
    commit_sha  TEXT NOT NULL,
    config_diff TEXT,
    time        TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS deploys_service_time_idx ON deploys (service, time DESC);
