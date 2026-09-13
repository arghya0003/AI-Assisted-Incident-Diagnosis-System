-- Grouped anomaly detections from both of M2's detectors (EWMA and the
-- frozen static-threshold comparison baseline - see
-- services/anomaly-detector/main.py). One row per flush-window group, not
-- one row per raw (service, metric) trip. `detector` distinguishes which
-- algorithm produced the row so the evaluation runner
-- (services/eval-runner/) can score them independently for the EWMA-vs-
-- static ablation the project plan asks for.
--
-- Only the `ewma` rows are also published to the anomalies.detected Kafka
-- topic (the contract M4 builds against, per CONTRACTS.md) - this table is
-- the durable, queryable record both detectors write to regardless.
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
