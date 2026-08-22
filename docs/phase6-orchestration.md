# Phase 6 Notes: Full Orchestration

## What this phase actually tested
Not just "does it currently work" (it did — Phases 0-5 were verified incrementally as
built) but the project's actual Definition of Done: **does `docker compose up` bring the
entire system online from a genuinely clean checkout, with zero manual steps.**

Every previous phase's verification ran against a stack that had been incrementally built
up over days — meaning some bugs a truly fresh checkout would hit were never actually
exercised. Found one real bug this way.

## Bug found: a Kafka topic auto-creation race
`metrics-bridge`, `metrics-sink`, and `deploy-emitter` only depended on `kafka-init`
*starting*, not *finishing*. `kafka-init` explicitly creates `metrics.raw` / `logs.raw` /
`deploys.events` with 3 partitions and 24h retention — but Kafka's own
`auto.create.topics.enable` defaults to `true` (never overridden here), so if any
producer/consumer touched a topic before `kafka-init` explicitly created it, Kafka would
silently auto-create it first with the wrong defaults (1 partition, 7-day retention),
permanently undermining the partitioning/retention decisions from Phase 3 — and nothing
would ever complain, since "the topic exists" is all Kafka checks.

Fixed by changing `depends_on` from the short form (just "has this container started")
to the long form with `condition: service_completed_successfully` for `kafka-init`, and
`condition: service_healthy` for `timescaledb` (which needed a `pg_isready` healthcheck
added — it didn't have one before). Confirmed the fix in the compose log itself: the fresh
bring-up shows `kafka-init-1 Exited` and `timescaledb-1 Healthy` *before*
`metrics-bridge-1 Starting` / `metrics-sink-1 Starting` / `deploy-emitter-1 Starting`.

## Proof it works
1. `docker compose down -v --remove-orphans` — full teardown, including the TimescaleDB
   data volume (simulates a clean clone; the wiped data is fully synthetic, nothing lost).
2. `docker compose up -d --build` — one command, ~3 minutes, no manual steps.
3. All 22 containers came up, zero restarts, `timescaledb` reporting `healthy`.
4. Verified both `deploys` and `metrics` tables existed (both SQL init files ran together
   automatically for the first time — previously `002_deploys.sql` had been applied by
   hand since it was added after the volume already existed).
5. Verified `metrics.raw` had the correct config (3 partitions, `retention.ms=86400000`) —
   not Kafka's auto-create defaults, confirming the race fix actually worked.
6. Generated real traffic, confirmed metrics landed in TimescaleDB (210 rows within
   seconds) and the deploy-emitter API worked (deploy numbering correctly restarted at
   `dep-2026-08-19-0001` on the fresh volume).

## Other cleanup done in this pass
- Removed a stray blank line in the `user-db` service definition (cosmetic).
- Header comments in `docker-compose.yml` updated to describe the current
  Sock Shop + Kafka + TimescaleDB + deploy-emitter architecture rather than referencing
  stale Train Ticket phase numbering.
