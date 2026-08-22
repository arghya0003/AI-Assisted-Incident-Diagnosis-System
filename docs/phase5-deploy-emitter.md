# Phase 5 Notes: Deploy Event Emitter

## Where the deploy log lives
Not a separate Postgres container — the architecture diagram groups "TimescaleDB
hypertables, continuous aggregates, PostgreSQL deploy log" under one "Time-Series Store"
box, so `deploys` is just another table in the same `metrics` database TimescaleDB already
runs (`timescaledb/init/002_deploys.sql`). No hypertable needed here — deploy volume is
orders of magnitude lower than metric samples, a plain indexed table
(`service, time DESC`) is enough.

Since the TimescaleDB data volume already existed from Phase 4, Postgres's
`/docker-entrypoint-initdb.d/` auto-run doesn't fire for new files against an
already-initialized volume — applied `002_deploys.sql` manually this once
(`psql -f ...`). It'll run automatically on any fresh checkout, though.

## Service: deploy-emitter
`services/deploy-emitter/` — a small Flask API + background loop, port 5000:

- **`POST /deploys`** `{service, version?, commit_sha?, config_diff?}` — the real trigger
  point. M2's fault-injection harness (Phase 8) will call this to record "a bad deploy just
  happened" immediately before injecting the matching fault, so M3 has something real to
  correlate against.
- **`GET /deploys?service=&limit=`** — recent deploy history, for debugging/verification
  and as a plain REST fallback if anyone needs it without touching Kafka or Postgres
  directly.
- Background thread fabricates one ordinary version-bump deploy to a random service every
  2 minutes (`SIMULATE_INTERVAL_SECONDS`), so the log isn't empty and flat until the
  fault-injection harness exists. Version auto-increments per service (queries the last
  recorded version, bumps the patch number, starts at `1.0.0`).

Every deploy — manual or simulated — goes through the same `create_deploy()` function,
which does both:
1. `INSERT ... RETURNING` into the `deploys` table (`deploy_id` generated from a Postgres
   sequence: `dep-YYYY-MM-DD-NNNN`, matching CONTRACTS.md's example format).
2. Publishes the same record onto `deploys.events` (CONTRACTS.md shape), keyed by
   `service`.

## Proof it works
Triggered two real deploys via the API:

```
POST /deploys {"service":"catalogue"}
→ {"deploy_id":"dep-2026-08-19-0001","service":"catalogue","version":"1.0.0",...}
POST /deploys {"service":"catalogue"}
→ {"deploy_id":"dep-2026-08-19-0002","service":"catalogue","version":"1.0.1",...}
```

Version bump confirmed correct (`1.0.0` → `1.0.1`). Confirmed both landed on
`deploys.events` via direct console-consumer read — exact CONTRACTS.md shape, real values.

## What this unblocks
M3's whole correlation signal depends on this existing — "was there a recent deploy to
this service (or one it calls) around the time this anomaly started" is the core question
M3's dependency-graph traversal needs to answer, and now there's real data to query.
