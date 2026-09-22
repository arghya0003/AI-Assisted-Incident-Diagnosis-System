-- Member 4 (orchestrator): the incident lifecycle and the immutable audit log behind the
-- safety architecture (PLAN.md, M4 responsibilities). Same timescaledb/metrics instance as
-- every other member's tables -- one Time-Series Store, per the architecture diagram.
--
-- Runs automatically only on a fresh database volume (standard Postgres entrypoint
-- behaviour). On an existing volume, apply by hand once, the same way as 005/006/007:
--   docker compose exec -T timescaledb psql -U postgres -d metrics -f - < timescaledb/init/008_incidents.sql
-- Safe to re-run (CREATE TABLE IF NOT EXISTS / CREATE OR REPLACE everywhere).

-- One row per anomaly the orchestrator opened an incident for. `anomaly` and `hypotheses`
-- keep a full snapshot of what M2 and M3 said at the time, so an incident's record does not
-- silently change meaning if either upstream table is later pruned or overwritten.
CREATE TABLE IF NOT EXISTS incidents (
    incident_id              TEXT PRIMARY KEY,
    anomaly_id               TEXT NOT NULL,
    state                    TEXT NOT NULL CHECK (state IN (
        'DETECTED', 'ANALYZING', 'ANALYSIS_FAILED', 'AWAITING_APPROVAL',
        'APPROVED', 'REJECTED', 'EXPIRED'
    )),
    services                  TEXT[] NOT NULL,
    severity                  TEXT NOT NULL,
    anomaly                   JSONB NOT NULL,
    analysis_id               TEXT,
    model_version             TEXT,
    answered_by               TEXT,
    hypotheses                JSONB NOT NULL DEFAULT '[]'::jsonb,
    analysis_attempts         INT NOT NULL DEFAULT 0,
    fail_reason               TEXT,
    -- Set only once a human decides; NULL the whole time the incident is open.
    decision                  TEXT CHECK (decision IN ('approved', 'rejected')),
    decided_hypothesis_rank   INT,
    decided_by                TEXT,
    decided_at                TIMESTAMPTZ,
    decision_reason           TEXT,
    -- True once the stubbed executor has logged intent for an APPROVED incident (app/executor.py
    -- never touches the running system -- see its module docstring for why that is a hard gate,
    -- not a policy one).
    execution_logged          BOOLEAN NOT NULL DEFAULT false,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    awaiting_since            TIMESTAMPTZ,
    expires_at                TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS incidents_state_idx ON incidents (state, created_at DESC);
CREATE INDEX IF NOT EXISTS incidents_anomaly_idx ON incidents (anomaly_id);
CREATE INDEX IF NOT EXISTS incidents_expires_idx ON incidents (state, expires_at)
    WHERE state = 'AWAITING_APPROVAL';

-- ---------------------------------------------------------------------------------------
-- Immutable audit log (PLAN.md safety architecture, item c). Append-only: who approved
-- what, when, on what evidence, with which model version. `hash` chains to `prev_hash`
-- (app/audit.py hash_entry) so the sequence is tamper-evident; the trigger below stops
-- ordinary SQL from rewriting a row. See app/audit.py's module docstring for exactly what
-- this guarantees and what it does not.
-- ---------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    audit_id     BIGINT PRIMARY KEY,
    incident_id  TEXT,
    event_type   TEXT NOT NULL,
    actor        TEXT NOT NULL,
    detail       JSONB NOT NULL,
    prev_hash    TEXT NOT NULL,
    hash         TEXT NOT NULL UNIQUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS audit_log_incident_idx ON audit_log (incident_id, audit_id);
CREATE INDEX IF NOT EXISTS audit_log_event_idx ON audit_log (event_type, created_at DESC);

CREATE OR REPLACE FUNCTION audit_log_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only: % of audit_id=% is not permitted', TG_OP, OLD.audit_id;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_log_no_update ON audit_log;
CREATE TRIGGER audit_log_no_update
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();

-- Rejection feedback as labelled data (PLAN.md safety architecture, item d): every reject
-- decision, with a coarse category alongside the free-text reason, so it can be aggregated
-- without NLP and later used to retrain scoring weights or prompts.
CREATE TABLE IF NOT EXISTS rejection_feedback (
    id                 BIGSERIAL PRIMARY KEY,
    incident_id        TEXT NOT NULL,
    anomaly_id         TEXT NOT NULL,
    hypothesis_rank    INT,
    reason_category    TEXT NOT NULL,
    reason             TEXT NOT NULL,
    approver           TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS rejection_feedback_category_idx ON rejection_feedback (reason_category, created_at DESC);
