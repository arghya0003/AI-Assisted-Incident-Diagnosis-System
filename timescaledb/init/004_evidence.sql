-- Unified diagnosis evidence store.
-- Each row references a source record and keeps category-specific details in payload.
-- This supports anomaly, metric, deployment, dependency, and similar-incident evidence.

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id  TEXT PRIMARY KEY,
    incident_id  TEXT NOT NULL,
    category     TEXT NOT NULL CHECK (category IN (
        'anomaly',
        'metrics',
        'deployment',
        'dependency',
        'similar_incident'
    )),
    source_id    TEXT NOT NULL,
    service      TEXT,
    observed_at TIMESTAMPTZ NOT NULL,
    relevance   DOUBLE PRECISION NOT NULL CHECK (relevance >= 0 AND relevance <= 1),
    summary     TEXT NOT NULL,
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS evidence_incident_relevance_idx
    ON evidence (incident_id, relevance DESC, observed_at DESC);

CREATE INDEX IF NOT EXISTS evidence_category_time_idx
    ON evidence (category, observed_at DESC);

CREATE INDEX IF NOT EXISTS evidence_service_time_idx
    ON evidence (service, observed_at DESC)
    WHERE service IS NOT NULL;
