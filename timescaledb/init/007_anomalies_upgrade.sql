-- Brings an existing `anomalies` table up to the shape in
-- 005_anomalies.sql (the one agreed with M3). Safe to run repeatedly, and a
-- no-op on a volume created after that file, so it can also sit in the init
-- directory without doing harm.
--
--   docker compose exec -T timescaledb psql -U postgres -d metrics \
--     -v ON_ERROR_STOP=1 -f /docker-entrypoint-initdb.d/007_anomalies_upgrade.sql
--
-- (In Git Bash, prefix with MSYS_NO_PATHCONV=1 so the container path is not
-- rewritten into a Windows one.)
--
-- Two things to know about rows written before this ran:
--
--   * their `raw` holds only the fields that had no column of their own,
--     not the whole event, because that is what the old `detail` column
--     stored. The columns still carry every frozen contract field, so
--     nothing is lost that a consumer needs;
--   * their `received_at` is the moment this migration ran, not when the
--     event arrived, because the column is backfilled with a default. Treat
--     `received_at - t_detected` as write lag only for rows stored after
--     this point.

DO $$
BEGIN
    IF to_regclass('public.anomalies') IS NULL THEN
        RAISE NOTICE 'no anomalies table yet; apply 005_anomalies.sql first';
        RETURN;
    END IF;

    -- detail -> raw (same JSONB column, clearer meaning)
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_name = 'anomalies' AND column_name = 'detail')
       AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'anomalies' AND column_name = 'raw') THEN
        ALTER TABLE anomalies RENAME COLUMN detail TO raw;
    END IF;
END $$;

ALTER TABLE anomalies ADD COLUMN IF NOT EXISTS raw JSONB;
ALTER TABLE anomalies ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'kafka';
ALTER TABLE anomalies ADD COLUMN IF NOT EXISTS received_at TIMESTAMPTZ NOT NULL DEFAULT now();

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'anomalies_source_check') THEN
        ALTER TABLE anomalies ADD CONSTRAINT anomalies_source_check
            CHECK (source IN ('kafka', 'fixture'));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'anomalies_severity_check') THEN
        ALTER TABLE anomalies ADD CONSTRAINT anomalies_severity_check
            CHECK (severity IN ('low', 'medium', 'high'));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'anomalies_window_check') THEN
        ALTER TABLE anomalies ADD CONSTRAINT anomalies_window_check
            CHECK (evidence_window_start <= evidence_window_end);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS anomalies_source_onset_idx ON anomalies (source, t_onset DESC);
