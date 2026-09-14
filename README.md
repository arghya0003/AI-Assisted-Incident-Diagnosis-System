# AI-Assisted Incident Diagnosis System

Capstone project repository. See [CONTRACTS.md](CONTRACTS.md) for the Kafka/REST schemas
every slice produces and consumes.

| Slice | Owner | Status |
| --- | --- | --- |
| Testbed & ingestion pipeline | M1 | Phases 0-6, 8 complete |
| Anomaly detection & evaluation | M2 | Detector and harness built; no testbed run yet |
| Retrieval-augmented reasoning | M3 | Not started |
| Orchestration, HITL UI & safety | M4 | Not started |

---

# Member 1: Testbed & Ingestion Pipeline

Data-plane owner for the capstone project. Nothing blocks this slice — M2/M3/M4 are blocked
on it, so it front-loads hard in weeks 1-6, then tapers into supporting M2's fault injection.

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

### Phase 7 (Week 6-7) — Integration support ◐ partially unblocked
Help M2 wire onto `metrics.raw` / TimescaleDB, help M3 get deploy-log and dependency-graph
read access. M2 now exists and consumes `metrics.raw`, `deploys.events` and the
`fault_scenarios` / `metrics` / `deploys` tables — see the Member 2 section below. M3 still
does not exist, so the deploy-log and dependency-graph handoff is still pending.

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
services/anomaly-detector/  Phase 9 (M2) — swappable detectors, grouping, deploy-window policy
services/evaluation-runner/ Phase 9 (M2) — fault injection + scoring; produces the report tables
results/                    Evaluation output (gitignored — regenerate, don't commit)
```

---

# Member 2: Anomaly Detection & Evaluation Framework

Owns "something is wrong" — and "how do we know we're right". Full design notes and the
reasoning behind each decision are in [docs/phase9-detection.md](docs/phase9-detection.md).

### Phase 9 — Detector ✅
`services/anomaly-detector/` rebuilt from a single-file EWMA z-score into four modules:

- **`detectors.py`** — four swappable detectors (`ewma`, `static`, `zscore`, `cusum`) behind
  one interface, selected by the `DETECTOR` env var. The comparison detectors exist so the
  evaluation can show EWMA earned its place rather than asserting it.
- **`grouping.py`** — collapses the dozen metric breaches one fault produces into a single
  `anomalies.detected` event with a member list, with a per-service cooldown so an ongoing
  fault does not re-alert every cycle.
- **`deploy_window.py`** — a service that is mid-deploy must clear a higher evidence bar.
  It never fully suppresses: hard-muting during a deploy would silence `bad_deploy_latency`,
  the most important fault class in the project.
- **`staleness.py`** — liveness. A crashed container disappears from Prometheus, so it
  emits *no* telemetry rather than bad telemetry; every per-sample detector is blind to it.
  Silence from a previously-healthy service is treated as its own high-severity signal.
- **`store.py`** — writes every emitted event to the `anomalies` table so M3 can look an
  anomaly up by ID after it has left the 24h Kafka topic. Written after the Kafka publish
  and never raises, so a database outage cannot hold an alert back.
- **`main.py`** — Kafka and database wiring only.

Three real bugs found and regression-tested: error-rate anomalies could never fire (a fixed
noise floor put 3-sigma above a ratio's maximum), a sustained fault was absorbed into the
baseline so the detector went quiet mid-incident, and CUSUM could never fire at all
(threshold crossings cleared the accumulator before the corroboration gate could pass it).

The liveness gap was found by the evaluation harness, not by the tests — the first live run
missed a `service_crash` completely. See [docs/phase9-detection.md](docs/phase9-detection.md).

### Phase 9 — Evaluation harness ✅ (built, not yet run against the testbed)
`services/evaluation-runner/` — the command every number in the final report comes from.

```bash
docker compose run --rm evaluation-runner live                      # inject faults, score the live detector
docker compose run --rm evaluation-runner live --suite smoke        # quick wiring check
docker compose run --rm evaluation-runner replay --since-minutes 120  # ablation over recorded data
```

`live` measures the real end-to-end pipeline. `replay` re-runs every detector over an
identical recorded stream, which is what makes the ablation a claim about detectors rather
than about testbed conditions. Reports land in `./results/` as Markdown and CSV.

Scoring separates *misattributed* (alerted, wrong service) from *missed* (never alerted),
because collapsing them would flatter the detector. Metrics that depend on M3's ranker —
root-cause accuracy, MRR, evidence validity — are implemented and tested but report
"not measured" rather than a zero that would read as a measured failure.

### Tests
99 tests, no Docker needed:

```bash
cd services/anomaly-detector   && python -m pytest tests -q   # 59
cd services/evaluation-runner  && python -m pytest tests -q   # 40
```

`test_replay.py` runs the real detector, grouper and scoring code over a synthetic metric
stream with a known fault — an end-to-end check of the whole chain.

### Results
Full seven-scenario suite against the live stack: **4/7 detected (80% of observable),
median latency 36.5s** against a 60s target, and **zero false positives** across 11.9
minutes of quiet observation. `service_crash` is 3/3.

**Ablation (18 scenarios, 26,370 replayed samples, identical input per detector):** EWMA,
CUSUM and 3-sigma all detect 12/18; a static threshold manages **8/18** and misses *every*
latency fault. Catalogue's p95 goes from a 5.9ms baseline to 222ms under CPU throttle — a
38x regression that a defensible 500ms global threshold sails straight past. No single fixed
threshold works across services with different healthy baselines; that is what EWMA buys.

The three non-detections are testbed limitations, not detector failures, and the harness
distinguishes them — see [docs/phase9-detection.md](docs/phase9-detection.md):

- `front-end` and `orders` serve no traffic (Sock Shop runs without a load generator), so
  they emit only cpu/memory and a latency fault there is undetectable by construction.
  Scored `unobservable`, but still counted against the headline rate.
- `db_pool_saturation` genuinely exhausts catalogue-db's 151-connection pool, yet catalogue
  is unaffected — it holds an established pool and never needs a new connection at 0.2
  req/s. The fault starves something the victim does not use.

### Not done yet
- **Four of seven services have no traffic**, which caps what the evaluation can cover, and
  means `error_rate` is never ingested for any service. Fixing it means adding a load
  generator to the shared testbed — M1's call, flagged for the team.
- Three fault classes, not the six in the plan — the injector cannot produce memory leak /
  OOM, dependency timeout cascade or config error yet.
- Seasonality suppression deliberately skipped (no diurnal cycle in synthetic traffic).
- No CI.

---

## Diagnosis evidence

The shared diagnosis evidence model groups supporting facts into anomaly, metrics,
deployment history, service dependencies, and similar past incidents. Its contract is
documented in `CONTRACTS.md`, with the database schema in
`timescaledb/init/004_evidence.sql`. See `docs/evidence-model.md` for the mapping to
the current data sources and the planned M3 diagnosis service.
