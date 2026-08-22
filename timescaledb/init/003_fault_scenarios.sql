-- Ground-truth record of every injected fault, matching CONTRACTS.md's
-- "eval hooks" test-fixture shape (scenario_id, fault_type,
-- ground_truth_service, t_inject) plus a couple of practical extras
-- (t_recovered, status, params) M2's evaluation runner will want when
-- scoring detection latency and false positives.
--
-- Same timescaledb/metrics instance as `metrics` and `deploys` - one
-- Time-Series Store, per the architecture diagram.

CREATE TABLE IF NOT EXISTS fault_scenarios (
    scenario_id          TEXT PRIMARY KEY,
    fault_type            TEXT NOT NULL,
    ground_truth_service  TEXT NOT NULL,
    t_inject              TIMESTAMPTZ NOT NULL,
    t_recovered           TIMESTAMPTZ,
    status                 TEXT NOT NULL DEFAULT 'running',
    params                 JSONB
);

CREATE INDEX IF NOT EXISTS fault_scenarios_service_time_idx
    ON fault_scenarios (ground_truth_service, t_inject DESC);
