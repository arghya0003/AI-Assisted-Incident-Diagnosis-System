# AI-Assisted Incident Diagnosis System

Capstone project repository. See [CONTRACTS.md](CONTRACTS.md) for the Kafka/REST schemas
every slice produces and consumes.

The system watches a microservice testbed for anomalies, ranks the likely root cause with
cited evidence, and puts a proposed remedy in front of a human to approve. It never acts on
the system itself: the executor is a logged stub by design.

| Slice | Owner | Status |
| --- | --- | --- |
| Testbed & ingestion pipeline | M1 | Phases 0-8 complete |
| Anomaly detection & evaluation | M2 | Complete: detector, evaluation harness, Incident Console and two held-out test sites. Sock Shop 6/7 faults detected (median 31.0s); the held-out sites 9/9 each, with false-positive rates above target. See the Member 2 section |
| Retrieval-augmented reasoning | M3 | Phases 0-8 complete (`services/diagnosis-service/`); evaluated across four pipeline modes and verified live |
| Orchestration, HITL UI & safety | M4 | Orchestrator + approval UI built and run end-to-end against the live stack |

## How the pieces fit

1. Sock Shop and the two dummy sites (StreamBox, BuildBoard) export Prometheus metrics.
   `metrics-bridge` publishes them to Kafka `metrics.raw`, and `metrics-sink` writes them to
   TimescaleDB. `deploy-emitter` records deploys to the `deploys` table and `deploys.events`.
2. `anomaly-detector` (M2) reads `metrics.raw`, raises an alert when a service departs from its
   baseline, and publishes `anomalies.detected`. Every alert is also stored in `anomalies`.
3. `orchestrator` (M4) consumes `anomalies.detected`, opens an incident, and calls
   `diagnosis-service` (M3) `POST /analyze`.
4. `diagnosis-service` ranks candidate services from the anomaly, the `deploys` table, a
   dependency graph and past incidents, and returns hypotheses that cite their evidence.
5. A human approves or rejects in `orchestrator-ui` (:3000) or the Incident Console (:5010).
   Approving logs intent; nothing is executed.
6. Alongside all that, `fault-injector` (M1) records labelled faults in `fault_scenarios`,
   `evaluation-runner` (M2) scores the detector and the ranker against them, and
   `eval-dashboard` shows the whole loop on one page.

## Running it

```bash
cp .env.example .env     # then add your own OPENROUTER_API_KEY, see below
docker compose up -d
```

Ports are listed below. The approval console is <http://localhost:3000>.

A plain `docker compose up -d` starts Sock Shop and the platform only. The two held-out
dummy sites, StreamBox and BuildBoard, start with their profiles (see Member 2):

```bash
docker compose -f docker-compose.yml -f docker-compose.sites.yml --profile streaming --profile cicd up -d
```

Both `-f` files are needed for every site command, because the site services are defined only in the second.
The base stack is 28 services; the sites add 13 (11 site services and 2 traffic generators).

**On an existing TimescaleDB volume**, Postgres does not re-run `timescaledb/init/*.sql`, so a
stack that has been up since an earlier phase is missing newer tables. The symptom is a service that
looks healthy but stores nothing — most visibly an approval console with no incidents in it. Apply
the migration and restart the service that owns the table:

```bash
# M4's incidents and audit log (the approval console)
docker compose exec -T timescaledb psql -U postgres -d metrics -v ON_ERROR_STOP=1   -f /docker-entrypoint-initdb.d/008_incidents.sql
docker compose restart orchestrator
```

Each script is safe to re-run. `GET /health` on the owning service reports
`"status": "degraded"` with the remedy in `detail` when its tables are missing, so you can check
rather than guess. M3's tables have their own instructions in
[services/diagnosis-service/README.md](services/diagnosis-service/README.md).

On Git Bash for Windows, prefix the command with `MSYS_NO_PATHCONV=1` and use
`//docker-entrypoint-initdb.d/...`, or the path is rewritten to a Windows one and psql reports
"No such file or directory".

### The API key, and why everyone needs their own

`diagnosis-service` writes its explanations with a cloud model, through
[OpenRouter](https://openrouter.ai). The key lives in `.env`, which is **gitignored and must stay
that way** — so cloning this repository does not give you one. Get your own free key in about two
minutes: sign in at <https://openrouter.ai> with GitHub, create a key at
<https://openrouter.ai/keys>, and **enable prompt logging under Settings → Privacy**, which the free
models require. Without that setting every request fails with "No endpoints found matching your data
policy", which looks exactly like a broken key. The prompts contain synthetic Sock Shop incidents
only.

Use your own key rather than sharing one: the free tier is 20 requests a minute and 50 a day per
account, so a shared key is a shared daily budget, and one evaluation run exhausts it for everyone.

**Nothing breaks without a key.** The stack comes up, `/analyze` still returns a ranked,
evidence-cited answer from the deterministic scorer, and the response header
`X-Diagnosis-Mode: deterministic_fallback` says so. You lose the written explanation, not the
diagnosis. `.env.example` documents the rest of the settings.

## Testbed layout

`testbed/` is gitignored — it's an upstream clone kept for reference only (nothing builds
from it, all images are pulled pre-built from Docker Hub) and it carries its own `.git`
history. Re-clone it if you need to inspect Sock Shop's source:
`git clone https://github.com/microservices-demo/microservices-demo testbed/sock-shop`

```
testbed/sock-shop/          Upstream Sock Shop clone (reference only — images are pre-built)
docker-compose.yml          Root skeleton — full stack: Sock Shop + Prometheus + Kafka + TimescaleDB
docker-compose.sites.yml   The two held-out dummy sites: StreamBox (profile `streaming`), BuildBoard (profile `cicd`)
prometheus/prometheus.yml   Scrape config for Phase 2 — targets each service's built-in /metrics
services/metrics-bridge/    Phase 3 — bridges Prometheus metrics onto the metrics.raw Kafka topic
services/metrics-sink/      Phase 4 — Kafka consumer writing metrics.raw into TimescaleDB
timescaledb/init/           Phase 4/5/8 — hypertable, continuous aggregate, deploy log, fault_scenarios schema
services/deploy-emitter/    Phase 5 — records deploys to Postgres + publishes deploys.events
services/fault-injector/    Phase 8 — real fault injection against the testbed via the Docker Engine API
services/load-generator/    Standing traffic through edge-router, so injected faults are observable
services/anomaly-detector/  Phase 9 (M2) — swappable detectors, grouping, deploy-window policy
services/evaluation-runner/ Phase 9 (M2) — fault injection + scoring; produces the report tables
services/dummy-service/    Phase 9-10 (M2) — one configurable service that plays every part of the two dummy sites
services/eval-dashboard/   Incident Console on :5010 (M2) — inject, decide, read history, detection rate
scripts/inci.ps1           PowerShell shortcuts for the stack, see Terminal shortcuts
docs/                      Per-phase write-ups; M2's are phase9-detection.md and phase10-held-out-sites.md
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
| 5010 | `eval-dashboard` — the Incident Console: inject a fault, decide on an incident, read the history and the detection rate |
| 8100 | `stream-web` — StreamBox, the streaming dummy site (profile `streaming`) |
| 8101 | `ci-web` — BuildBoard, the CI/CD dummy site (profile `cicd`) |
| 8081 | `kafka-ui` — debugging only (profile `debug`) |
| 8082 | `adminer` — debugging only (profile `debug`) |
| 9090 | `prometheus` |
| 29092 | `kafka` — host-side listener |

## Terminal shortcuts

```powershell
.\scripts\install-shortcuts.ps1     # once, from anywhere you cloned this
```

Then `InciWeb` opens the console, `SockShopWeb` the application under test, and
`InciDemo` every demo tab in presenting order. `InciHelp` lists the rest.

Two of them do more than open a page, because the same things keep going wrong:

- **`InciUp`** builds before it starts. Container images do not track this
  repository, and three separate defects here were a merged fix that was not in
  the running image - each time the symptom looked like a different bug.
- **`InciStatus`** reports the offered request rate (a fault with no traffic
  moves no metric and nothing can detect it), whether the orchestrator's Kafka
  consumer is consuming (that thread can die while the API stays healthy, so
  incidents silently stop opening), and whether a Gemini key is present (without
  one, every diagnosis quietly comes from the deterministic ranker).

PowerShell only. The installer writes to the profile of whichever shell runs it,
which matters because VS Code often launches PowerShell 7 while the standalone
app is Windows PowerShell 5.1, and they read different profiles.

The held-out sites have their own: `InciStream` and `InciCI` open their pages,
`InciStreamUp` and `InciCIUp` start them, and `InciDown` stops everything, both sites
included. `InciDemo` does not open the site pages yet.

`kafka-ui` and `adminer` are debugging UIs that nothing else depends on, so they sit
behind a Compose profile and stay off by default (~300 MiB between them). Bring them up
with `docker compose --profile debug up -d`.

## Tests, all slices

Run on 2026-11-10 from a checkout of this branch, no Docker needed:

| Service | Owner | Result |
| --- | --- | --- |
| `anomaly-detector` | M2 | 64 passed |
| `evaluation-runner` | M2 | 149 passed |
| `dummy-service` | M2 | 45 passed, 1 skipped on Windows (it reads `/proc`) |
| `eval-dashboard` | M2 | 55 passed |
| `fault-injector` | M1 | 34 passed |
| `load-generator` | M1 | 7 passed |
| `diagnosis-service` | M3 | 506 passed, 15 skipped, 4 expected failures |
| `orchestrator` | M4 | 63 passed |

`metrics-bridge`, `metrics-sink` and `deploy-emitter` have no unit tests; they are exercised
against the live stack. `orchestrator-ui` is checked by `npm run lint` and `npm run build`,
which were not re-run for this table.

`.github/workflows/ci.yml` runs the eight Python suites above on every pull request, the
console's lint and build, and a check that `docker-compose.yml` is valid.

## Open across the project

Each is described in its owner's "Not done yet" section. They are gathered here because
none can be settled by one member alone.

- **Which harness owns the ranking numbers.** M2's `attribute` and M3's live scorer both
  compute MRR and top-k. The report should use one.
- **Attribution under cascades.** When a fault cascades, the first alert often names the caller,
  not the broken service. On the held-out sites, top-3 and MRR fall below target once those
  cases count. Whether M2's grouper or M3's ranker should disambiguate is undecided.
- **False positives on the held-out sites** are above target (StreamBox 4.36/h, BuildBoard
  7.28/h), and the cause is not established (M2).
- **The retrieval corpus is empty on a fresh volume.** It needs `--ingest` and Ollama (M3).
- **The LLM path has not been exercised end to end** with `answered_by=llm` on the
  verification machine (M4).
- **Executing an approved action is out of scope by design** (M4).

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

### Phase 7 (Week 6-7) — Integration support ✅
Help M2 wire onto `metrics.raw` / TimescaleDB, help M3 get deploy-log and dependency-graph
read access. M2 consumes `metrics.raw`, `deploys.events` and the
`fault_scenarios` / `metrics` / `deploys` tables — see the Member 2 section below. M3
(`services/diagnosis-service/`) reads the `deploys` table directly and keeps its own
dependency graph in `config/dependency_graph.yaml`, so that handoff is done.

### Phase 8 (Week 7+) — Fault injection harness (pulled forward) ✅
Skipped ahead to the part of Phase 8 that doesn't depend on teammates: a real
fault-injection harness (`services/fault-injector/`, port 5001) against the testbed, via
the Docker Engine API. Three scenario types — `bad_deploy_latency` (CPU-throttle + a
companion deploy event), `service_crash` (stop/restart), `db_pool_saturation` (real held
MySQL connections against `catalogue-db`) — all verified with real, physical effects (p95
latency hit 7.47s during a throttle test; Prometheus itself independently reported a
crashed service as down). Ground truth recorded in `fault_scenarios`, in CONTRACTS.md's
eval-hooks shape, which M2's evaluation runner now scores against. CI runs on every pull
request (`.github/workflows/ci.yml`, added by M4). See `docs/phase8-fault-injection.md`.

**Update (issue #35):** the injector now produces all seven fault classes. It adds
`dependency_timeout` (paused dependency), `config_error` (dependency resolved to loopback),
`memory_exhaustion` (OOM via cgroup limit) and `resource_exhaustion` (gradual CPU ramp).
`db_pool_saturation` now holds a table lock: the held connections it used before never
reached catalogue, which reuses two pooled connections. Each was verified on the live stack,
and faults interrupted by an injector restart are undone at startup.

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
- **[#12] `queue-master` ran unbounded and the debug UIs started by default** — the only
  one of the four Spring Boot services without a `JAVA_OPTS` heap cap, it sized its heap
  from the whole WSL VM and sat at ~600 MiB, about twice its siblings. Given the same cap
  they use. `kafka-ui` and `adminer` are debugging tools nothing depends on, so they moved
  behind the `debug` Compose profile (`docker compose --profile debug up -d`) rather than
  costing ~300 MiB on every start — headroom that matters because the evaluation host also
  runs Ollama. **Verified live:** `queue-master` went 635 → 282 MiB, in line with carts
  (260) and shipping (284), and reattached to `shipping-task` with no backlog; the stack is
  now 3.0 GiB across 23 containers. `kafka`'s heap is left alone deliberately — it is
  load-bearing and wants measuring under load first.

Also found while verifying, and **not** fixed here — each needs its owner's call:

- `cpu_limit` defaults to `0.002` now, not `0.05`. A CPU quota only bites below what the
  service actually uses, and catalogue idles at 0.17% of a core, so `0.05` was a no-op —
  measured, p95 flat through a full 90 s throttle. `evaluation-runner`'s suite now passes
  `0.002` for catalogue and `0.005` for front-end/orders; the latter two are estimates
  that still need a measured run (M2).
- `error_rate` is never ingested for any service: with no 5xx anywhere, the PromQL series
  doesn't exist and the bridge skips the sample instead of publishing 0 (M1).
- The `anomalies` table is missing on any stack whose TimescaleDB volume predates
  `005_anomalies.sql`, because Postgres only runs `init/` on a fresh volume. Applied by
  hand on this stack; the general fix is an idempotent migration step (M1).

---

# Member 2: Anomaly Detection & Evaluation Framework

Owns "something is wrong" — and "how do we know we're right". Full design notes and the
reasoning behind each decision are in [docs/phase9-detection.md](docs/phase9-detection.md).

**Status: complete.** It covers the detector, the evaluation harness, the Incident Console
(`services/eval-dashboard`), and two held-out dummy sites (`services/dummy-service`) that the
detector and ranker were never tuned on. The held-out results and their caveats are in
[docs/phase10-held-out-sites.md](docs/phase10-held-out-sites.md).

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

### Phase 9 — Evaluation harness ✅ (run against the live testbed, with and without standing traffic)
`services/evaluation-runner/` — the command every number in the final report comes from.

```bash
docker compose run --rm evaluation-runner live                      # inject faults, score the live detector
docker compose run --rm evaluation-runner live --suite smoke        # quick wiring check
docker compose run --rm evaluation-runner live --suite streaming    # held-out site, needs its site stack up
docker compose run --rm evaluation-runner live --suite cicd         # held-out site, needs its site stack up
docker compose run --rm evaluation-runner attribute --since-minutes 120  # ranker metrics over the same window
docker compose run --rm evaluation-runner replay --since-minutes 120  # ablation over recorded data
```

A site suite refuses to start unless every service it touches reported metrics in the last two minutes.
A site that is down would otherwise be faulted and scored as a string of misses.

`live` measures the real end-to-end pipeline. `replay` re-runs every detector over an
identical recorded stream, which is what makes the ablation a claim about detectors rather
than about testbed conditions. Reports land in `./results/` as Markdown and CSV.

Scoring separates *misattributed* (alerted, wrong service) from *missed* (never alerted),
because collapsing them would flatter the detector. Metrics that depend on M3's ranker —
root-cause accuracy, MRR, evidence validity — are implemented and tested but report
"not measured" rather than a zero that would read as a measured failure.

### Tests
No Docker needed. Counts are from `pytest --collect-only` on 2026-11-10:

```bash
cd services/anomaly-detector   && python -m pytest -q   # 64
cd services/evaluation-runner  && python -m pytest -q   # 149
cd services/dummy-service       && python -m pytest -q   # 46; one is skipped on Windows (it reads /proc)
cd services/fault-injector      && python -m pytest -q   # 34
cd services/eval-dashboard      && python -m pytest -q   # 55
```

`test_replay.py` runs the real detector, grouper and scoring code over a synthetic metric
stream with a known fault — an end-to-end check of the whole chain.

### Results
Full seven-scenario suite against the live stack **with standing traffic** (2026-09-23):
**6/7 detected (86%), median latency 31.0s** against a 60s target, nothing scored
`unobservable` and nothing misattributed. `bad_deploy_latency` and `service_crash` are both
3/3; the only miss is `db_pool_saturation`. The two fastest detections — `front-end` at
23.1s and `orders` at 17.9s — are scenarios that were undetectable by construction before
M1's load generator existed. `error_rate` reached the detector for the first time (286
samples, peaking at 1.8%), so the noise-floor fix is finally testable against live data.

**Ablation (same window, 15,062 replayed samples, identical input per detector):** EWMA,
CUSUM and 3-sigma each detect **6/7**; a static threshold manages **4/7**. Under load
`orders` sits at about 240ms while `user` sits at about 5ms, so no single fixed threshold
can serve both — which is exactly what a per-(service, metric) learned baseline buys.

**That result depends on how hard the fault is, so it was swept** (issue #34,
`evaluation-runner sweep`). Five magnitudes of `bad_deploy_latency` on `catalogue`, each
resetting the testbed first and each replayed through every detector:

| measured p95 impact | `ewma` | `static` | `zscore` | `cusum` |
| --- | --- | --- | --- | --- |
| 0.0 ms | 1/1 | **0/1** | 1/1 | 1/1 |
| 59.6 ms | 1/1 | **0/1** | 1/1 | 1/1 |
| 533.9 ms | 1/1 | 1/1 | 1/1 | 1/1 |
| 801.4 ms | 1/1 | 1/1 | 1/1 | 1/1 |
| 804.6 ms | 1/1 | 1/1 | 1/1 | 1/1 |

**The crossover is 534 ms** — and the static detector's configured threshold is 500 ms, so
the experiment independently recovered a number that was set in configuration. Below it a
learned baseline catches faults a fixed threshold does not; above it the choice of detector
stops mattering. That is the honest form of the claim, and it is a more useful finding than
either single run: *a learned baseline earns its place on subtle regressions.*

Two caveats travel with it. At the 0 ms rung the adaptive detectors fired on
`latency_p99_ms`, not p95 — a gentle throttle lifts the tail first — so the x-axis
understates what they actually saw at that end. And each rung is one scenario, so the
direction is clear across five rungs but no rung carries an error bar.

**Before the load generator (2026-09-13), for comparison:** 4/7 (57%), median 36.5s, two
scenarios unobservable; replay over 18 scenarios gave 12/18 for the three adaptive
detectors against 8/18 for the static one. Both runs are kept in
[docs/phase9-detection.md](docs/phase9-detection.md) — the difference between them is the
most useful thing the harness has produced.

**The false-positive rate is 0.95/hour, and meets its target** (2026-10-05, 1 alert across
63.0 minutes of quiet observation, testbed reset first, no machine sleep in the window —
the three conditions that invalidated every earlier attempt). Target is under 1/hour.

**It was 5.72/hour three days earlier, and that gap is only partly explained.** That
measurement was honest: 6 alerts in 63 quiet minutes, of which four were `orders` reporting
a genuine degradation — p95 climbing 41ms to 105ms and holding for half an hour. Neither
explanation below is a change to the detector, and the first does not account for those
four alerts:

- **The cause of the 2026-10-02 `orders` degradation is not established.** The load
  generator's pruner (#33, #39) had merged the day before and was already running, so an
  unbounded order history does not explain that window. A separate two-hour run with the
  count held at ~52 documents kept `orders` p95 flat at 42-52ms with no alerts. That shows
  `orders` can stay flat; it does not show why it failed to on 2026-10-02.
- The detector stopped alerting on the testbed's own instrumentation. M1 added
  `orders-db`'s `document_count` and `prune_rate` as metrics and added both to the
  detector's ignore list in the same commit — but only half of that commit was running,
  because the detector image predated it. For an hour, **13 of 19 alerts** were the
  pruner's own batch-delete rate being read as a service degrading.

The second one is the more useful lesson, and it is recorded here rather than quietly
fixed: a merged change that is not in the running image fails in a way that looks like a
different bug entirely. Three separate defects during this evaluation had that same cause.

**How far these numbers go.** 0.95/hour is one window containing one alert, so it passes
narrowly: a second alert in the same window would have been 1.90/hour. The plan's five
metrics have also never been measured together. Detection latency (27.6 s) comes from the
11-scenario suite of 2026-10-02, which reports the three ranker metrics as not measured.
Top-3 100%, MRR 1.00 and evidence validity 100% come from an earlier `attribute` report the
same day over 16 scenarios, on the deterministic ranker rather than the LLM, and top-1 falls
to 94% once cascades are included. `attribute` has not been run on the window with all six
fault classes. Quote each figure with its own run, not as one combined result.

### Held-out test sites ✅ (run 2026-11-07)
StreamBox and BuildBoard exist to test the detector and ranker on systems they were never
tuned on. Both run the same `dummy-service` image, configured by environment, with real
synchronous downstream calls, so a fault cascades the way it would in a real system.

- **StreamBox** (`stream-*`, port 8100): `stream-web` calls `stream-catalog` and
  `stream-playback`; playback calls `stream-cache`, which calls `stream-storage`.
- **BuildBoard** (`ci-*`, port 8101): `ci-web` → `ci-pipeline` → `ci-scheduler` →
  `ci-runner` (two slots), and `ci-pipeline` → `ci-artifacts` → `ci-deployer`.

Each site carries 5 req/s of its own traffic and has no edge to Sock Shop. The detector
groups alerts per site, so one site's alert never merges into another's.

Full suite, one run each, detector untouched (labelled `ewma-untuned`), nine faults at
five-minute gaps:

| Metric | Target | StreamBox | BuildBoard |
| --- | --- | --- | --- |
| Faults detected | — | 9 of 9 | 9 of 9 |
| Detection latency, median (p95) | median under 60 s | 13.7 s (39.6 s) | 34.7 s (40.0 s) |
| Top-3, alert names the injected service | above 70% | 100% | 89% |
| Top-3, first alert in the window, any service | above 70% | 67% | 56% |
| MRR, alert names the injected service | above 0.6 | 0.87 | 0.81 |
| MRR, first alert in the window, any service | above 0.6 | 0.48 | 0.43 |
| Evidence validity | 100% | 100% | 100% |
| False-positive rate | under 1 per hour | **4.36/h** (3 in 41.3 min) | **7.28/h** (5 in 41.2 min) |

The false-positive target and the unfiltered ranking targets are **not met** on either site.
The filtered rows only count cases where the alert already names the injected service, so they
are an upper bound. Nothing has been tuned. Any later run with changed thresholds is tuned and
must say so. The write-up, with per-fault latencies, rankings and caveats, is in
[docs/phase10-held-out-sites.md](docs/phase10-held-out-sites.md); the four generated reports are in
[docs/held-out-runs/2026-11-07/](docs/held-out-runs/2026-11-07/).

### Incident Console (`services/eval-dashboard`, port 5010)
One page for the whole loop. It is a small server that proxies every call, because none of the
backends sets CORS headers, and it keeps the database password out of the browser.

| Tab | What it shows |
| --- | --- |
| **Recent** | One row per anomaly, newest first: its cause, the diagnosis, the fix a human approved, and whether it ended. A system filter (Sock Shop, StreamBox, BuildBoard) runs in the query. At the 100-row cap the Anomalies tile reads "100+". |
| **Inject a fault** | System, then fault type, then service. Every service in the system stays listed; those the fault cannot target are greyed out with the reason. |
| **Decisions** | Approve, reject or request more information on an incident awaiting a human. |
| **History** | One row per injected fault: whether it was detected, how fast, what the ranker blamed, and what a human decided. |
| **Detection rate** | Injected against detected, per fault class, over time. |

The form's rules mirror the injector's `params.py`. `services/eval-dashboard/tests/test_catalog.py`
reads that file and fails if the two disagree.

### Not done yet
- ~~The false-positive rate is uncharacterised.~~ Measured at **0.95/hour** over one
  63-minute window (2026-10-05). The earlier 5.72/hour reading was also honest, and the
  degradation behind it is still unexplained; see above.
- **The testbed may still degrade on its own.** `orders` latency tracked the order history
  while the load generator never pruned it, and two hours at a held document count showed
  p95 flat at 42-52ms after bounding it (#33, #39). But `orders` degraded again on
  2026-10-02 with the pruner running, so bounding the history did not settle this. Also
  unexplained is the recovery
  in the original observation: with nothing pruning, the count only grew, so latency
  returning to 48ms unaided does not fit a pure count-drives-latency model. It is the
  natural experiment
  for separating CUSUM from EWMA — which tie on every injected fault so far.
- **`db_pool_saturation` still misses, and traffic did not fix it.** At 2.2 req/s
  catalogue's p95 stayed at exactly 4.8ms through the whole fault window: it reuses an
  established pool, so exhausting the database's capacity to accept *new* connections never
  touches it. Either the injector kills catalogue's existing connections, or the scenario
  should be dropped.
- ~~Three fault classes injectable, not the six in the plan.~~ The injector now produces all
  seven classes (M1's update, issue #35). The runner asks it what it supports before a run,
  so an unimplemented class is skipped rather than scored as a miss.
- ~~MRR / top-k / evidence validity are unscored by this harness (issue #21).~~
  `evaluation-runner attribute` now asks the diagnosis service about the anomaly raised
  inside each fault window and scores the ranking against the injected service: **top-1
  92%, top-3 100%, MRR 0.94, evidence validity 100%** over 12 scorable scenarios. Two
  caveats. Rankings are read from the `hypotheses` table rather than the `/analyze`
  response, because the response schema carries no service field while the table does; and
  the single top-1 miss was an anomaly where the grouper had bundled the injected service
  with `orders`, which was genuinely degrading (#33) — so top-1 currently measures M2's
  grouping as much as M3's ranking. M3 has a separate live scorer (PR #31) using the same
  MRR definition; the team should still settle which harness owns the number.
- **Attribution under cascades is an open question.** With real traffic an anomaly often
  names the caller rather than the broken service (M3, PR #31). Whether M2 should order
  `services[]` by likely culpability, or M3 should disambiguate, is undecided.
- **The held-out sites raise false positives above target.** StreamBox 4.36/h and BuildBoard
  7.28/h against under 1/h, from one run each. A 40-minute quiet window on a fresh stack gave
  none, so it is not yet clear whether the alerts come from fault aftermath, host load or the
  detector. Five of BuildBoard's came within 20 s of alerts on another system.
- **Site rankings miss outside the filtered view.** Filtered top-3 meets its target on both
  sites. Unfiltered top-3 and MRR do not (StreamBox 67% and 0.48, BuildBoard 56% and 0.43).
  The misses are cascades, where the symptom sits upstream of the cause.
- **`config_error` on `stream-web` was missed on 2026-11-01 and detected on 2026-11-07.** The
  first run had 2.5 minutes between faults and the second had 5. That is not a proven cause.
- **`db_pool_saturation` has not been re-measured** since M1 changed it to a table lock.
- Seasonality suppression deliberately skipped (no diurnal cycle in synthetic traffic).
- ~~No CI.~~ M4 added `.github/workflows/ci.yml`, which runs both M2 suites on every pull
  request alongside M3's and M4's, plus a compose validation.
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
asks an LLM to explain the ranking, validates the reply, and stores the run.
`GET /candidates/{id}` shows the deterministic ranking behind any answer,
`GET /hypotheses/{id}` lists stored runs, `GET /stats` the guardrail counters.

**Generation goes to Gemini** (`gemini-flash-latest`), falling back across vendors to OpenRouter
(`nvidia/nemotron-3-super-120b-a12b:free`). Keys live in the gitignored `.env` at the repo root as
`GEMINI_API_KEY` and `OPENROUTER_API_KEY`. The chain crosses providers on purpose: Gemini returns
transient 503s under load while OpenRouter exhausts a 50-a-day free quota, and those two are
unlikely to fail together. Without any key the service still answers, always from the deterministic
ranking. `LLM_PROVIDER=ollama` switches generation back to a local `phi4-mini`, which is how the
phi4-mini results below can be reproduced.

**Embeddings stay local**: `nomic-embed-text` through Ollama on the host, reached at
`host.docker.internal` (`OLLAMA_HOST=0.0.0.0` is required). The corpus itself needs no ingestion
step — its embeddings ship with it — but retrieval embeds the anomaly *query* at request time, so
without Ollama `retrieval_status` is `embedding_unavailable` and incident similarity scores 0.

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
downstream/upstream traversal, plus the two dummy sites (11 nodes, 9 edges): 25 nodes and
23 edges in all, with no edge between systems. No LLM.

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
Built on phi4-mini, since moved to OpenRouter (see below), with **JSON-schema constrained
decoding** throughout: each hypothesis is bound to one listed
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

A host run on 2026-11-10 reports 506 passed, 15 skipped and 4 expected failures. The Docker
script runs the suite and the optional steps:

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

**The ablation, on real injected faults** (2026-10-01, `scripts/eval_live.py`, issue #22). Nine
scenarios across all seven fault classes, 8 detected, 24 scored runs:

| Mode | top-1 | MRR | evidence validity | p50 latency |
| --- | --- | --- | --- | --- |
| `full` | **7/8** | **0.88** | 100% | 20.0 s |
| `llm_only` | 5/8 | 0.69 | 100% | 22.5 s |
| `deterministic` | **7/8** | **0.88** | 100% | **0.20 s** |

Median detection delay 33.9 s, inside the plan's 60 s target. **Repeated on a second model**
(Gemini `gemini-flash-latest`, 2026-10-02): `full` 6/8 · MRR 0.81, `llm_only` 4/8 · 0.62,
`deterministic` 6/8 · 0.81. The `deterministic` baseline moved between runs despite calling no model,
so the scenario set got harder and absolute numbers are not comparable across runs — but each run
agrees with itself: `full` equals `deterministic` exactly in both, and `llm_only` loses by exactly 2
scenarios and 0.19 MRR in both. The scaffolding is worth about two scenarios in nine regardless of
which model is behind it. **Retrieval and scoring beat the model on its own**, and the two `llm_only` misses share one shape: given the facts with no graph, scores or
past incidents, it blamed the caller `front-end` where `full` named the crashed service. front-end
appears in nearly every cascade, and nothing weighs it down. **The LLM still adds no ranking
accuracy** — `full` and `deterministic` agree scenario for scenario at about 100 times the latency.
One class is systematically wrong: `dependency_timeout` pauses `catalogue-db` and every mode answered
`catalogue`. Earlier live runs on 2026-09-28 scored 1/3 and 2/3; those were depressed by a stale
injector image, uncleaned `orders` history (issue #33) and a fallback model that never answered, not
by the problem being harder than fixtures.

**M2's `liveness` signal solved the crash case.** A crashed service used to be invisible to
scoring — it stops reporting, so it was never anomalous and never ranked. The staleness
detector names it directly, and the existing weights then rank it first, in all four modes.

## Not done yet

- **The corpus is empty on a fresh volume** (issue #19). Ingestion needs `--ingest` and
  Ollama, so a clean `docker compose up` still runs a retrieval-free system — but it is no
  longer silent: `GET /health` reports `corpus_incidents` and a `retrieval` status.
- **Accuracy, MRR and evidence validity are measured here on fixtures.** M2's harness now
  also scores them against injected faults via `POST /analyze` (issue #21), so there are
  two sources for the same three metrics and the team should settle which the report uses.
- **The ablation runs on fixtures, not real injected faults** (issue #22).
- **`scale_service` is never proposed.** A deliberate choice, not an oversight: nothing
  measured here shows a service is overloaded, so offering it would be guessing.
  `restart_service` *is* proposed, but only to a service M2's staleness detector reports
  silent (issue #23, resolved).
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
63 tests, no Docker needed:

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
- ~~Integration/CI workflow (GitHub Actions) not added yet.~~ `.github/workflows/ci.yml` now
  runs the Python suites, the console's lint and build, and a compose check on every pull
  request.
- Executing an approved action is deliberately out of scope (PLAN.md's risk register: "the
  executor is architecturally stubbed by design ... listed as future work, not a stretch
  goal")
