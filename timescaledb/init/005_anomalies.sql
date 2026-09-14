-- Durable record of every anomalies.detected event, one row per grouped
-- incident, keyed by the same anomaly_id. Written by
-- services/anomaly-detector/store.py right after each Kafka publish.
--
-- Why it exists: a Kafka topic is a 24h stream, so nothing can look an
-- anomaly up by ID once it scrolls past - and M3's POST /analyze
-- {anomaly_id} needs exactly that lookup. Kafka stays the live contract
-- M4 consumes; this table is the queryable copy.
--
-- `detector` is the algorithm that produced the event (ewma, zscore, cusum,
-- static, or staleness for a service that stopped reporting). `detail`
-- holds the event's additive fields whole - contributors,
-- in_deploy_window, related_deploy_ids - so a new field on the event needs
-- no schema change.
--
-- Runs automatically only when the database volume is first created. On an
-- existing volume, apply it once by hand:
--   docker compose exec -T timescaledb psql -U postgres -d metrics < timescaledb/init/005_anomalies.sql
--
-- Same timescaledb/metrics instance as `metrics`, `deploys`, and
-- `fault_scenarios` - one Time-Series Store, per the architecture diagram.

CREATE TABLE IF NOT EXISTS anomalies (
    anomaly_id             TEXT PRIMARY KEY,
    detector                TEXT NOT NULL,
    services                TEXT[] NOT NULL,
    metrics                 TEXT[] NOT NULL,
    severity                TEXT NOT NULL,
    t_onset                 TIMESTAMPTZ NOT NULL,
    t_detected              TIMESTAMPTZ NOT NULL,
    evidence_window_start   TIMESTAMPTZ NOT NULL,
    evidence_window_end     TIMESTAMPTZ NOT NULL,
    detail                  JSONB
);

CREATE INDEX IF NOT EXISTS anomalies_detected_idx ON anomalies (t_detected DESC);
CREATE INDEX IF NOT EXISTS anomalies_detector_idx ON anomalies (detector);
CREATE INDEX IF NOT EXISTS anomalies_services_idx ON anomalies USING GIN (services);
