-- Durable record of every anomalies.detected event, one row per grouped
-- incident, keyed by the same anomaly_id. Written by
-- services/anomaly-detector/store.py right after each Kafka publish.
--
-- Why it exists: a Kafka topic is a 24h stream, so nothing can look an
-- anomaly up by ID once it scrolls past - and M3's POST /analyze
-- {anomaly_id} needs exactly that lookup. Kafka stays the live contract
-- M4 consumes; this table is the queryable copy.
--
-- Shape agreed with M3, who had built the same table independently. Taking
-- the better half of each design:
--
--   * `raw` keeps the published event verbatim (M3's design). An earlier
--     version stored only the fields without a column of their own, which
--     meant rebuilding an event needed both the columns and the JSON, and
--     silently dropped anything M2 added later.
--   * `detector` records which algorithm fired - ewma, zscore, cusum,
--     static, or staleness for a service that stopped reporting (M2's
--     design). Needed to tell an ablation run's rows apart.
--   * `source` separates hand-written fixtures from real events (M3's
--     design), because evaluation must exclude fixtures.
--   * `received_at` is when the row was stored, so write lag behind
--     `t_detected` is measurable.
--
-- The frozen contract fields keep real columns, so ordinary queries need no
-- JSON at all; `raw` is there for whole-event reads and for fields added
-- after this table was written.
--
-- Runs automatically only when the database volume is first created. On an
-- existing volume, apply 007_anomalies_upgrade.sql instead - it is
-- idempotent and brings an older anomalies table up to this shape.
--
-- Same timescaledb/metrics instance as `metrics`, `deploys`, and
-- `fault_scenarios` - one Time-Series Store, per the architecture diagram.

CREATE TABLE IF NOT EXISTS anomalies (
    anomaly_id              TEXT PRIMARY KEY,
    detector                TEXT NOT NULL,
    services                TEXT[] NOT NULL,
    metrics                 TEXT[] NOT NULL,
    severity                TEXT NOT NULL
        CONSTRAINT anomalies_severity_check CHECK (severity IN ('low', 'medium', 'high')),
    t_onset                 TIMESTAMPTZ NOT NULL,
    t_detected              TIMESTAMPTZ NOT NULL,
    evidence_window_start   TIMESTAMPTZ NOT NULL,
    evidence_window_end     TIMESTAMPTZ NOT NULL,
    raw                     JSONB NOT NULL,
    source                  TEXT NOT NULL DEFAULT 'kafka'
        CONSTRAINT anomalies_source_check CHECK (source IN ('kafka', 'fixture')),
    received_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT anomalies_window_check CHECK (evidence_window_start <= evidence_window_end)
);

CREATE INDEX IF NOT EXISTS anomalies_detected_idx ON anomalies (t_detected DESC);
CREATE INDEX IF NOT EXISTS anomalies_detector_idx ON anomalies (detector);
CREATE INDEX IF NOT EXISTS anomalies_services_idx ON anomalies USING GIN (services);
-- M3 looks for other anomalies that began near this one's onset, never
-- mixing fixtures with real events, so the two columns are indexed together.
CREATE INDEX IF NOT EXISTS anomalies_source_onset_idx ON anomalies (source, t_onset DESC);
