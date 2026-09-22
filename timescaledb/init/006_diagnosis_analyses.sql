-- Member 3 (diagnosis-service), Phase 8. Additive only: one row per /analyze run, and two columns
-- tying stored hypotheses to their run. Apply to an existing volume the same way as 005 (see its
-- header), with -f /docker-entrypoint-initdb.d/006_diagnosis_analyses.sql. Safe to re-run.

-- One row per /analyze run, including runs that produced no hypotheses (an llm_only failure), so
-- evaluation can count failures, latency and fallbacks per pipeline mode in plain SQL.
CREATE TABLE IF NOT EXISTS analyses (
    analysis_id         TEXT PRIMARY KEY,
    anomaly_id          TEXT NOT NULL,
    pipeline_mode       TEXT NOT NULL CHECK (pipeline_mode IN ('full', 'llm_only', 'no_graph', 'deterministic')),
    -- llm: phi4-mini's answer; deterministic: the scorer's ranking by design (deterministic mode);
    -- deterministic_fallback: the scorer's ranking because the LLM failed; llm_failed: no answer.
    answered_by         TEXT NOT NULL CHECK (answered_by IN ('llm', 'deterministic', 'deterministic_fallback', 'llm_failed')),
    model_version       TEXT NOT NULL,     -- the LLM model, or 'none' in deterministic mode
    config_fingerprint  TEXT NOT NULL,     -- hash of everything that shapes an answer (app/analyses.py)
    llm_attempts        INT NOT NULL,
    guardrail_rejected  INT NOT NULL,
    latency_ms          INT NOT NULL,
    fallback_reason     TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- The response cache looks up the newest reusable run for this key.
CREATE INDEX IF NOT EXISTS analyses_cache_idx
    ON analyses (anomaly_id, pipeline_mode, model_version, config_fingerprint, created_at DESC);

ALTER TABLE hypotheses ADD COLUMN IF NOT EXISTS analysis_id TEXT;
-- The candidate a hypothesis is about. Not in the /analyze contract, but it lets evaluation compare a
-- rank-1 service with ground truth in SQL.
ALTER TABLE hypotheses ADD COLUMN IF NOT EXISTS service TEXT;
CREATE INDEX IF NOT EXISTS hypotheses_analysis_idx ON hypotheses (analysis_id, rank);
