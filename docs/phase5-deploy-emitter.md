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
  `SIMULATE_INTERVAL_SECONDS` (default 15 minutes — see below), so the log isn't empty and
  flat until the fault-injection harness exists. Version auto-increments per service
  (queries the last recorded version, bumps the patch number, starts at `1.0.0`). Set it
  to `0` to turn the loop off entirely and leave only deploys recorded through
  `POST /deploys`.

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

## The simulated rate is an evaluation parameter (issue #7)
The loop ran every **120 s** originally, which was picked to make the log non-empty
quickly, without thinking about what it does downstream. M3 measured the effect: 102
deploys in 3h 22m across 7 services means each service gets one about every 14 minutes, so
a given service has a deploy inside M3's 30-minute lookback roughly **90%** of the time,
and within 7 minutes of any moment about 40% of the time. Deploy correlation is the
strongest signal in M3's root-cause scoring, and at that rate it is mostly noise: a
routine background deploy scored 0.41 on a real catalogue latency anomaly, and in M3's
scoring tests a background deploy to a *symptom* service outranked the true cause of a
DB-saturation fault.

Changed the default to **900 s** (15 min). That keeps the log realistically non-empty
without putting a fabricated deploy inside almost every lookback window. It stays a
per-run knob, deliberately:

| `SIMULATE_INTERVAL_SECONDS` | Use |
| --- | --- |
| `900` (default) | Ordinary runs — background deploys present but not dominant |
| `120` | Deliberate stress condition; report it as such in the evaluation methodology |
| `0` | Loop off — only injected deploys in the log, the cleanest diagnosis measurement |

Whatever a run uses has to be recorded with its results, because diagnosis accuracy is not
comparable across values. `GET /healthz` reports the live setting
(`simulate_interval_seconds`, `simulated_deploys_enabled`) so an evaluation run can capture
it instead of assuming the default was in effect.

M3 won't filter simulated deploys out on its side, and shouldn't: injected deploys go
through the same `create_deploy()` path, and real systems don't label their deploys
harmless.

## What this unblocks
M3's whole correlation signal depends on this existing — "was there a recent deploy to
this service (or one it calls) around the time this anomaly started" is the core question
M3's dependency-graph traversal needs to answer, and now there's real data to query.
