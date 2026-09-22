# AI-Assisted Incident Diagnosis System

Capstone project repository. See [CONTRACTS.md](CONTRACTS.md) for the Kafka/REST schemas
every slice produces and consumes.

| Slice | Owner | Status |
| --- | --- | --- |
| Testbed & ingestion pipeline | M1 | Phases 0-6, 8 complete |
| Anomaly detection & evaluation | M2 | Detector and harness built and run against the live testbed |
| Retrieval-augmented reasoning | M3 | Phases 0-8 complete (`services/diagnosis-service/`); evaluated across four pipeline modes and verified live |
| Orchestration, HITL UI & safety | M4 | Orchestrator + approval UI built and run end-to-end against the live stack |

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
  record in `docs/phase0-decisions.md`. Its `user-sim` was later replaced by a load
  generator of our own, for the reason in issue #6 below.
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
Kafka in KRaft mode, topics `metrics.raw` / `logs.raw` / `deploys.events` /
`anomalies.detected` created (3 partitions, 24h retention, keyed by `service`). Built
`metrics-bridge` (Python) to bridge Phase 2's Prometheus metrics onto `metrics.raw` in the
CONTRACTS.md shape — verified real
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
every `SIMULATE_INTERVAL_SECONDS` so the log isn't empty. Every deploy is written to the
`deploys` table (same TimescaleDB instance, per the architecture diagram) and published to
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
services/load-generator/    Standing traffic through edge-router, so injected faults are observable
services/anomaly-detector/  Phase 9 (M2) — swappable detectors, grouping, deploy-window policy
services/evaluation-runner/ Phase 9 (M2) — fault injection + scoring; produces the report tables
results/                    Evaluation output (gitignored — regenerate, don't commit)
```

## Ports

| Port | What |
| --- | --- |
| 80 | `edge-router` — the testbed's entry point |
| 5000 | `deploy-emitter` — `POST/GET /deploys`, `/healthz` |
| 5001 | `fault-injector` — `POST/GET /faults`, `/fault-types` |
| 5002 | `load-generator` — `/stats` (what load is actually being offered) |
| 8000 | `diagnosis-service` — `POST /analyze`, `/docs` |
| 8090 | `orchestrator` — incident REST API, `/ws` live feed, `/docs` |
| 3000 | `orchestrator-ui` — the HITL approval console |
| 8081 | kafka-ui · 8082 adminer · 9090 Prometheus · 29092 Kafka (host-side) |

## Fixes on top of the phase work

Issues raised by M3 against this slice, now addressed:

- **[#5] `anomalies.detected` wasn't created by `kafka-init`** — it was auto-created by
  Kafka on M2's first publish with 1 partition and 7-day retention, so anomalies outlived
  the 24h of metrics they refer to. Added to the topic list; `kafka-init` also converges
  topics that already exist, so a running dev stack is repaired by `docker compose up`
  rather than a volume wipe. **Verified live:** `--describe` now reports 3 partitions and
  `retention.ms=86400000`, converged on an existing topic without a volume wipe. See
  `docs/phase3-kafka-ingestion.md`.
- **[#6] No standing traffic, so injected faults changed no metric** — the testbed sat at
  ~0.2 req/s and 4 of 7 services emitted no latency data at all, which made the whole
  detect/diagnose/evaluate chain unmeasurable. Added `services/load-generator/`: a
  fixed, known, open-loop load over the whole call graph, with `GET /stats` so a run can
  prove traffic was flowing. The fault injector now records the offered rate on every
  scenario. **Verified live:** all 7 scraped services now report `latency_p95_ms` (was 3),
  load holds at 4.99 of a 5 req/s target with zero failures, and an injected throttle took
  catalogue from 4.8 ms to 160 ms p95 — which M2's detector then fired on, grouped with
  `front-end` and tagged to the companion deploy. See `docs/load-generator.md`.
- **[#7] A simulated deploy every 2 minutes drowned the signal** — deploy correlation is
  M3's strongest root-cause signal, and at that rate ~90% of services had a background
  deploy inside the lookback window. Default raised to 900s, `0` disables it, and the rate
  is reported by `GET /healthz` so an evaluation run records what it ran under. **Verified
  live:** deploys now land 900 s apart. See `docs/phase5-deploy-emitter.md`.

Also found while verifying, and **not** fixed here — each needs its owner's call:

- `cpu_limit` defaults to `0.002` now, not `0.05`. A CPU quota only bites below what the
  service actually uses, and catalogue idles at 0.17% of a core, so `0.05` was a no-op —
  measured, p95 flat through a full 90 s throttle. `evaluation-runner`'s suite passes
  `0.05`/`0.10` explicitly and needs its own look (M2).
- `error_rate` is never ingested for any service: with no 5xx anywhere, the PromQL series
  doesn't exist and the bridge skips the sample instead of publishing 0 (M1).
- The `anomalies` table is missing on any stack whose TimescaleDB volume predates
  `005_anomalies.sql`, because Postgres only runs `init/` on a fresh volume. Applied by
  hand on this stack; the general fix is an idempotent migration step (M1).

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
101 tests, no Docker needed:

```bash
cd services/anomaly-detector   && python -m pytest tests -q   # 61
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
  means `error_rate` is never ingested for any service. ~~Fixing it means adding a load
  generator to the shared testbed — M1's call, flagged for the team.~~ **M1 added one**
  (`services/load-generator/`, issue #6). Every result in this section was measured before
  it existed, so the detection rates, the `unobservable` scorings for `front-end`/`orders`
  and the `db_pool_saturation` finding all need re-running under standing load.
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
the current data sources and `services/diagnosis-service/` (M3), which produces it.

---

# Member 3: Retrieval-Augmented Reasoning

Turns one anomaly into ranked, evidence-cited root-cause hypotheses. Build plan and
per-phase outcomes are in [services/diagnosis-service/PLAN.md](services/diagnosis-service/PLAN.md);
full measurements in [services/diagnosis-service/README.md](services/diagnosis-service/README.md).

One service, `services/diagnosis-service/` (Python/FastAPI, port 8000). `POST /analyze
{anomaly_id}` reads M2's `anomalies` table, gathers context, ranks candidate services,
asks phi4-mini to explain the ranking, validates the reply, and stores the run.
`GET /candidates/{id}` shows the deterministic ranking behind any answer,
`GET /hypotheses/{id}` lists stored runs, `GET /stats` the guardrail counters.

Ollama runs on the **host**, not in a container, because it needs the GPU: `phi4-mini`
for generation and `nomic-embed-text` for embeddings, reached through
`host.docker.internal` (`OLLAMA_HOST=0.0.0.0` is required).

## Phased plan

### Phase 0 — Environment proof ✅
Scripted checks of the stack, the database and the LLM before building on them. Found the
first-load runner crash (HTTP 500, recovered on retry) that Phase 6's client handles.

### Phase 1 — Skeleton service ✅
Contract-validated `/analyze` stub. Every response is validated against the CONTRACTS.md
models, so shape drift fails here rather than in M4's UI.

### Phase 2 — Storage and fixtures ✅
11 hand-written anomaly fixtures (`fixtures/anomalies/*.json`), each an M2-shaped event
plus a `_fixture` block holding ground truth and the deploys and co-anomalies the scenario
would have produced. They are the ground truth every later phase is tested against.

### Phase 3 — Dependency graph ✅
Sock Shop's call graph (14 nodes, 14 edges) from `config/dependency_graph.yaml`, with
downstream/upstream traversal. No LLM.

### Phase 4 — Candidate scoring ✅
The deterministic ranker, and the baseline the LLM is measured against. Four signals,
weighted: deploy proximity 0.40 (exponential decay from onset), graph proximity 0.25,
co-anomaly 0.20 (is this the deepest anomalous service?), incident similarity 0.15.
Every candidate carries the evidence ids behind its score.

### Phase 5 — Retrieval (RAG) ✅
61-incident corpus (`corpus/incidents/`), embedded with `nomic-embed-text` into pgvector.
Hybrid by default: a structured pre-filter on candidate services and plausible fault types,
then cosine similarity. Only the *symptoms* are embedded — embedding whole write-ups let a
few generic ones match almost every query.

### Phase 6 — LLM reasoning ✅
phi4-mini with **JSON-schema constrained decoding**: each hypothesis is bound to one listed
candidate, with only that candidate's citable evidence ids and permitted actions. Text
instructions were not enough — with a flat action list the model proposed rolling back one
service's deploy as the fix for another. Three validate-and-retry attempts, then the
deterministic ranking as fallback, so `/analyze` never returns an invalid answer.

### Phase 7 — Evidence guardrail ✅
Pure set membership: any hypothesis citing an id that was not supplied to the model is
dropped. Poisoned replies citing `ev-9999` or `dep-fake-001` never reach a response — a
partly poisoned reply keeps only its clean hypothesis, a fully poisoned one returns the
deterministic ranking.

### Phase 8 — Persistence, modes and evaluation ✅
Every run stored in `analyses`/`hypotheses` (`timescaledb/init/006_diagnosis_analyses.sql`),
with a response cache keyed by anomaly, mode, model and a fingerprint of everything that
shapes an answer. Four pipeline modes — `full`, `no_graph`, `llm_only`, `deterministic` —
selectable per request, so the LLM's contribution can be measured rather than assumed.

## Tests

445 tests in Docker, 430 on the host without a database:

```bash
bash services/diagnosis-service/scripts/test_in_docker.sh   # + --ingest --fixtures --compare --eval
```

Covers scoring signal by signal, graph traversal, retrieval pre-filter and ranking, prompt
construction and the context budget, reply validation and retry, the guardrail against
poisoned replies, persistence and cache reuse, and every route via `TestClient`.

## Results

**The LLM does not rank better than the arithmetic.** 99 runs, 11 fixtures, 3 runs per mode
(2026-09-16):

| Mode | Rank-1 = true cause | Fell back to scorer | p50 latency |
| --- | --- | --- | --- |
| `deterministic` | **18/27** | — | **51 ms** |
| `full` | **18/27** | 2/33 | 14.2 s |
| `no_graph` | 17/27 | 9/33 | 16.6 s |

Fixture by fixture, `full` and `deterministic` are right and wrong on the same cases: the
LLM follows the scorer's ranking and writes the explanation. An earlier single-run pass
suggested `no_graph` beat `full`; three runs showed that was noise. In a separate run,
`llm_only` — the model given the same facts with no scoring, graph or retrieval — was worst
(4/9) and proposed 7 rollbacks in 11 runs, including on benign and ambiguous cases where
every scored mode chose `no_action`.

**What the graph actually buys is reliability, not accuracy.** Removing it raised the
fallback rate from 6% to 27% of runs: without graph positions the model invents deploys and
fails validation.

**Verified live end to end.** With M1's load generator holding 5 rps, a `service_crash`
injected on payment was reported by M2's staleness detector 31 s later as a `liveness`
anomaly; `/analyze` ranked payment first, answered by the LLM (3 attempts, 21.5 s, no
guardrail rejections), with the cause "the payment service stopped reporting liveness
metrics, indicating a possible crash or unreachability".

**M2's `liveness` signal solved the crash case.** A crashed service used to be invisible to
scoring — it stops reporting, so it was never anomalous and never ranked. The staleness
detector names it directly, and the existing weights then rank it first, in all four modes.

## Not done yet

- **The corpus is empty on a fresh volume** (issue #19). Ingestion needs `--ingest` and
  Ollama, so a clean `docker compose up` silently runs a retrieval-free system.
- **Accuracy, MRR and evidence validity are not produced by the evaluation harness**
  (issue #21) — measured here on fixtures, but the runner does not yet call `/analyze`.
- **The ablation runs on fixtures, not real injected faults** (issue #22).
- **`restart_service` and `scale_service` are never proposed** (issue #23): only
  `no_action` and `rollback_deploy` are reachable today.
- Causes are model-written text. They are constrained and checked for unsupported deploy
  claims, but they are not a verified explanation of the fault.

---

# Member 4: Orchestration, HITL UI & Safety

Owns the system being a system, and owns the safety story. Full design notes and the
reasoning behind each decision are in
[docs/phase-m4-orchestration.md](docs/phase-m4-orchestration.md).

Two services:

- **`services/orchestrator/`** (Python/FastAPI, port 8090) — consumes `anomalies.detected`
  (M2), calls M3's `POST /analyze`, and persists each incident through
  `DETECTED -> ANALYZING -> AWAITING_APPROVAL -> APPROVED/REJECTED/EXPIRED` (or
  `ANALYSIS_FAILED`, with a `/reanalyze` retry, if M3 could not be reached after its own
  retries). REST API plus a `GET /ws` live feed for the approval UI.
- **`services/orchestrator-ui/`** (React + TypeScript + Tailwind, port 3000) — the approval
  console: anomaly evidence, ranked hypotheses with their evidence chain and blast radius,
  and explicit Approve / Reject / Request-more-info actions. Every cited evidence id is
  clickable, resolving to the deploy diff, anomaly event or past postmortem behind it.

### Safety architecture
The headline contribution, built as code and enforced by tests, not asserted in prose:

- **Constrained action space** — the same fixed grammar M3 already emits
  (`rollback_deploy:<id>` | `restart_service:<service>` | `scale_service:<service>` |
  `no_action`), never free text. Resolves CONTRACTS.md's open question on the action
  vocabulary. `GET /actions` exposes it, with each verb's blast radius, for the UI.
- **A hard execution gate** — `app/executor.py`'s `execute()` is a log line: no
  Docker/Kubernetes/HTTP client, no subprocess, nothing reachable to call. A test parses the
  module's own AST and fails the build if an outbound-capable import is ever added. The
  orchestrator container also mounts no Docker socket and holds no infra credentials.
- **An immutable audit log** — `audit_log` (`timescaledb/init/008_incidents.sql`) rejects
  `UPDATE`/`DELETE` via a trigger, and every row hash-chains to the one before it
  (`GET /audit/verify` walks the chain). Documented as tamper-*evidence*, not
  tamper-*prevention* — its limitation is stated explicitly, not assumed away.
- **Rejection feedback as labelled data** — every reject requires a coarse reason category
  alongside the free-text reason, stored for a future retraining pass.

### Tests
53 tests, no Docker needed:

```bash
cd services/orchestrator && python -m pytest -q
```

Covers the full state machine (idempotent anomaly intake, analysis success/failure/retry,
approve/reject/request-info/expiry), the action-vocabulary validator, the audit hash-chain
(including a tamper-detection test), the executor's lack of outbound capability, and the
FastAPI routes end to end via `TestClient` with an in-memory fake store — the same pattern
`services/diagnosis-service`'s own test suite uses.

The frontend (`services/orchestrator-ui/`) builds clean: `npm run build` (TypeScript +
Vite) and `npm run lint` (oxlint) both pass.

### Verified against the live stack
`docker compose up -d --build` (23 containers), then one full scenario end to end:

- `bad_deploy_latency` injected against `catalogue`; M2's EWMA detector fired ~10 s later
  (target: under 60 s) and published to `anomalies.detected`.
- The orchestrator opened an incident, called M3, and reached `AWAITING_APPROVAL`. The
  rank-1 hypothesis named catalogue's real deploy `dep-2026-09-22-0001` at 0.843 confidence
  with resolvable evidence ids, proposing `rollback_deploy:dep-2026-09-22-0001` — the actual
  ground truth of the injected fault.
- `answered_by=deterministic_fallback` (no Ollama on that machine), so the LLM-unavailable
  path is covered, not just the happy path.
- Approved via the API: `execution_logged=true`, and the `catalogue` container's start time
  was unchanged afterwards — the stubbed executor never touched it.
- A second incident (`service_crash` on `user`, caught by M2's staleness detector) was
  rejected; the row landed in `rejection_feedback` with its category.
- The WebSocket feed pushed `incident_awaiting_approval` to a connected client in real time.
- Audit immutability holds at the database level, not just in the app: direct SQL `UPDATE`
  and `DELETE` against `audit_log` were both refused by the trigger, and `GET /audit/verify`
  reported the hash chain intact throughout.
- Evidence resolution was checked against a real incident's own cited ids: the deploy id
  returned its recorded `config_diff` (`"perf regression: inefficient loop introduced"`, the
  diff the injector wrote for the bad deploy), the anomaly id returned its event, a corpus
  postmortem returned its body, and the `metrics`/`dependency` forms resolved without a
  database read — including through the UI's nginx `/api` proxy.

This run is also what surfaced the `incidents` table-name collision with M3
(`orchestrator_incidents` now), which no amount of unit testing would have caught.

### Not done yet
- Ollama was not available on the verification machine, so the LLM path itself
  (`answered_by=llm`) has only been exercised through M3's own test suite, not end to end.
- Integration/CI workflow (GitHub Actions) not added yet — PLAN.md lists this under M4 too.
- Executing an approved action is deliberately out of scope (PLAN.md's risk register: "the
  executor is architecturally stubbed by design ... listed as future work, not a stretch
  goal")
