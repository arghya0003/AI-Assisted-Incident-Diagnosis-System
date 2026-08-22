# AI-Assisted Incident Diagnosis System — Member 1: Testbed & Ingestion Pipeline

Data-plane owner for the capstone project. Nothing blocks this slice — M2/M3/M4 are blocked
on it, so it front-loads hard in weeks 1-6, then tapers into supporting M2's fault injection.

See [CONTRACTS.md](CONTRACTS.md) for the Kafka/REST schemas this slice produces and consumes.

## Phased plan

### Phase 0 — Decisions ✅
- Benchmark app: **Sock Shop** (switched from Train Ticket — see
  `docs/phase0-decisions.md` for why: Train Ticket's build/discovery friction wasn't worth
  it for this stage; Sock Shop ships pre-built images with no build step).
- Service subset: full Sock Shop minus the load generator (14 services, real multi-hop
  dependency chain, verified against each image's actual source/config) — see decision
  record in `docs/phase0-decisions.md`.
- Docker Desktop confirmed working locally.
- Root `docker-compose.yml` skeleton in place — every other member's service plugs in here.
- Interface contracts drafted in `CONTRACTS.md` — **needs team sign-off before Week 3.**

### Phase 1 (Week 1) — Testbed stand-up ✅
Sock Shop is healthy end-to-end via `docker compose up`: all 14 containers up with zero
restarts, verified with a real request through `edge-router` → `front-end` → `catalogue`
→ `catalogue-db` returning HTTP 200 with real data.

### Phase 2 (Week 1-2) — Instrumentation ✅
Turned out every Sock Shop service already ships a native Prometheus `/metrics` endpoint
(latency histograms, CPU, memory) — no Micrometer/OTel agent needed. Added a Prometheus
server to scrape all 7 working services; verified p95 latency and request volume with
real traffic. See `docs/phase2-instrumentation.md` for the full writeup, including the
`queue-master` JSON-format gap and deferred DB/queue-infra metrics.

### Phase 3 (Week 2-3) — Kafka ingestion pipeline ✅
Kafka in KRaft mode, topics `metrics.raw` / `logs.raw` / `deploys.events` created (3
partitions, 24h retention, keyed by `service`). Built `metrics-bridge` (Python) to bridge
Phase 2's Prometheus metrics onto `metrics.raw` in the CONTRACTS.md shape — verified real
records landing on the topic via direct console-consumer read. `logs.raw` and
`deploys.events` exist (schemas frozen) but have no producer yet — see
`docs/phase3-kafka-ingestion.md` for why that's deliberately deferred.

### Phase 4 (Week 3-4) — TimescaleDB sink ✅
Built `metrics-sink` (Python, Kafka consumer group) writing `metrics.raw` into a
TimescaleDB hypertable, plus a 1-minute continuous aggregate and retention policies (24h
raw, 7d rolled-up). Proof: measured end-to-end lag directly in SQL at ~4.3-4.8s, steady
state, under the 5s target — see `docs/phase4-timescaledb.md`.

### Phase 5 (Week 4-5) — Deploy event emitter ✅
Built `deploy-emitter` (Flask API, port 5000): `POST /deploys` to record a real deploy,
`GET /deploys` to query history, plus a background loop fabricating an ordinary deploy
every 2 minutes so the log isn't empty. Every deploy is written to the `deploys` table
(same TimescaleDB instance, per the architecture diagram) and published to
`deploys.events`. Verified both paths with real API calls — see
`docs/phase5-deploy-emitter.md`.

### Phase 6 (Week 5-6) — Full Docker Compose orchestration ✅
Actually tested the project's Definition of Done, not just assumed it: full teardown
(`docker compose down -v`, including the data volume) then one command
(`docker compose up -d --build`) from nothing. Found and fixed a real bug this way — a
Kafka topic auto-creation race where `metrics-bridge`/`metrics-sink`/`deploy-emitter`
could touch a topic before `kafka-init` explicitly created it with the right
partitioning/retention, silently falling back to Kafka's wrong defaults. Fixed with proper
`depends_on` completion/health conditions. Verified clean: 22/22 containers up, zero
restarts, schema auto-initialized, topic config correct, real traffic flowing end-to-end.
See `docs/phase6-orchestration.md`.

### Phase 7 (Week 6-7) — Integration support ⬜ blocked
Help M2 wire onto `metrics.raw` / TimescaleDB, help M3 get deploy-log and dependency-graph
read access. Can't start — M2/M3 don't exist as code yet, nothing to integrate with.

### Phase 8 (Week 7+) — Fault injection harness (pulled forward) ✅ partial
Skipped ahead to the part of Phase 8 that doesn't depend on teammates: a real
fault-injection harness (`services/fault-injector/`, port 5001) against the testbed, via
the Docker Engine API. Three scenario types — `bad_deploy_latency` (CPU-throttle + a
companion deploy event), `service_crash` (stop/restart), `db_pool_saturation` (real held
MySQL connections against `catalogue-db`) — all verified with real, physical effects (p95
latency hit 7.47s during a throttle test; Prometheus itself independently reported a
crashed service as down). Ground truth recorded in `fault_scenarios`, in CONTRACTS.md's
eval-hooks shape, ready for M2's evaluation runner whenever it exists. Integration
testing/CI not started yet. See `docs/phase8-fault-injection.md`.

## Testbed layout

`testbed/` is gitignored — it's an upstream clone kept for reference only (nothing builds
from it, all images are pulled pre-built from Docker Hub) and it carries its own `.git`
history. Re-clone it if you need to inspect Sock Shop's source:
`git clone https://github.com/microservices-demo/microservices-demo testbed/sock-shop`

```
testbed/sock-shop/          Upstream Sock Shop clone (reference only — images are pre-built)
docker-compose.yml          Root skeleton — full stack: Sock Shop + Prometheus + Kafka + TimescaleDB
prometheus/prometheus.yml   Scrape config for Phase 2 — targets each service's built-in /metrics
services/metrics-bridge/    Phase 3 — bridges Prometheus metrics onto the metrics.raw Kafka topic
services/metrics-sink/      Phase 4 — Kafka consumer writing metrics.raw into TimescaleDB
timescaledb/init/           Phase 4/5/8 — hypertable, continuous aggregate, deploy log, fault_scenarios schema
services/deploy-emitter/    Phase 5 — records deploys to Postgres + publishes deploys.events
services/fault-injector/    Phase 8 — real fault injection against the testbed via the Docker Engine API
```
