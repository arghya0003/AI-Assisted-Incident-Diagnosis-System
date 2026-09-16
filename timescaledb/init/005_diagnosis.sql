-- Member 3 (diagnosis-service). Additive only: creates M3's own tables and the pgvector
-- extension, and touches nothing created by 001-004.
--
-- The `anomalies` table is NOT here: M2 owns it (timescaledb/init/005_anomalies.sql), writes
-- every published event to it, and agreed its shape with M3 in PR #10. This service only reads
-- it, and writes fixture rows through scripts/load_fixtures.py.
--
-- Init scripts only run on a fresh volume. To apply this to an existing one:
--   docker compose exec -T timescaledb psql -U postgres -d metrics -v ON_ERROR_STOP=1 \
--     -f /docker-entrypoint-initdb.d/005_diagnosis.sql
-- (In Git Bash, prefix with MSYS_NO_PATHCONV=1 so the container path isn't rewritten.)
-- Every statement is IF NOT EXISTS, so re-running it is harmless.

-- Past-incident corpus for retrieval. pgvector is available in this image (verified Phase 0).
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS incidents (
    incident_id   TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    body          TEXT NOT NULL,
    services      TEXT[],
    fault_type    TEXT,
    source        TEXT,              -- 'public_postmortem' | 'synthetic'
    embedding     vector(768),       -- nomic-embed-text; dimension verified in Phase 0
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Every hypothesis ever returned: the audit trail M4 links to, and the record the
-- evaluation scores against. pipeline_mode and model_version exist from the start so
-- ablation runs are a config flag rather than a schema change.
CREATE TABLE IF NOT EXISTS hypotheses (
    hypothesis_id    TEXT PRIMARY KEY,
    anomaly_id       TEXT NOT NULL,
    rank             INT NOT NULL,
    cause            TEXT NOT NULL,
    confidence       DOUBLE PRECISION NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    evidence_ids     TEXT[] NOT NULL,
    proposed_action  TEXT NOT NULL,
    model_version    TEXT NOT NULL,     -- e.g. 'phi4-mini@q4_K_M'
    pipeline_mode    TEXT NOT NULL,     -- 'full' | 'llm_only' | 'no_graph' | 'deterministic'
    latency_ms       INT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS hypotheses_anomaly_rank_idx ON hypotheses (anomaly_id, rank);
