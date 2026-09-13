# Member 3 — AI Reasoning & RAG Service (`diagnosis-service`)

**Owner:** Member 3. **Branch:** `member-3-ai-reasoning-rag`.
**Slice:** answer *"why is it wrong"* — given an `anomaly_id`, produce a ranked,
evidence-cited root-cause hypothesis list with a proposed (never executed) remediation.

This document is the build spec. Work one phase at a time, verify the phase's
**Definition of Done**, commit, then move on. Do not start a later phase before an earlier
one is verified — Phases 3 and 4 are deterministic and cheap to test now, and debugging
them later with LLM variability layered on top is much harder.

---

## 0. Where this slice sits

```
M1 ---> metrics.raw -----> metrics-sink ---> TimescaleDB (metrics, metrics_1m)
    \--> deploys.events ---------------> TimescaleDB (deploys)
                                            |
M2 ---> anomalies.detected -----------------+------> [THIS SERVICE]
                                            |            |
                                   reads deploys,        | POST /analyze
                                   metrics, evidence     v
                                                    M4 orchestrator / UI
```

**What I consume**

| Source | Shape | Where |
| --- | --- | --- |
| `anomalies.detected` (Kafka) | `{anomaly_id, services[], metrics[], severity, t_detected, t_onset, evidence_window{start,end}}` | M2, already live in `services/anomaly-detector/` |
| `deploys` table | `deploy_id, service, version, commit_sha, config_diff, time` | M1, `timescaledb/init/002_deploys.sql` |
| `metrics` / `metrics_1m` | `time, service, metric, value, labels` | M1, `timescaledb/init/001_schema.sql` |
| `evidence` table | `evidence_id, incident_id, category, source_id, service, observed_at, relevance, summary, payload` | M1 built this **for me**: `timescaledb/init/004_evidence.sql` |
| Dependency graph | Sock Shop topology | `CONTRACTS.md`, section "Service dependency graph" |

**What I produce**

- `POST /analyze` returning `{hypotheses: [{rank, cause, confidence, evidence_ids[], proposed_action}]}`
- Rows in `evidence` (I am the only writer of `category='similar_incident'`)
- Rows in `hypotheses` (my own table — the audit record M4 links to)

---

## 1. Locked technical decisions

| Decision | Choice | Rationale |
| --- | --- | --- |
| Language / runtime | **Python 3.11** | Every existing service in this repo is Python (`kafka-python`, `psycopg2`, Flask). Java/Spring AI would make me the only JVM service in the stack and the only one needing a Maven build step. |
| Web framework | **FastAPI + Pydantic** | Pydantic models *are* the JSON-schema validator for LLM output — the single most important guardrail in my slice. Also gives free OpenAPI docs for M4 to build against. (`deploy-emitter` uses Flask; the divergence is justified by the validation requirement.) |
| Generation model | **`phi4-mini`** (3.8B, Q4_K_M, ~2.5 GB VRAM) via Ollama | 4 GB VRAM ceiling. Reasoning-dense training and comparatively reliable structured/JSON output at this size. **Provisional** — Phase 0 benchmarks it on real hardware before anything is designed around it. |
| Embedding model | **`nomic-embed-text`** (768-dim, ~300 MB VRAM) | Fits alongside `phi4-mini` in 4 GB without contention. |
| Context window | **8192 tokens, hard cap** | Context competes with model weights for the same 4 GB. Prompt assembly must truncate deliberately, not hope. |
| Vector search | **pgvector** in the existing `metrics` DB, `vector(768)` with the `<=>` cosine operator | Confirmed available in M1's TimescaleDB image in Phase 0, so no image change and no NumPy fallback are needed. |
| Anomaly lookup | My own `anomalies` table, populated by my own Kafka consumer | `anomalies.detected` is pub/sub with no shared table (unlike `deploys`/`metrics`). `POST /analyze` only receives an `anomaly_id`, so I must have persisted it myself. **This is an architecture decision not yet in CONTRACTS.md — raise it with the team.** |
| Action vocabulary | Fixed enum: `rollback_deploy`, `restart_service`, `scale_service`, `no_action` | CONTRACTS.md open question 3. Formatted in responses as `<action>:<target_id>`, e.g. `rollback_deploy:dep-2026-08-12-0007`. Confirm with M4. |
| Executor | **None. Ever.** | Remediation is text for a human to approve. M4 owns the stubbed executor. |

### Open items to raise with the team (before the Week 5 contract freeze)

1. **Anomaly persistence** — does M3 subscribe and keep a local copy (my assumption), or does M1/M2 add a shared `anomalies` table? My design works either way; the team should pick one explicitly.
2. **`proposed_action` vocabulary** — confirm the four-value enum above with M4 (CONTRACTS.md lists this as unresolved).
3. **`severity` enum values** — confirm with M2 (`high`/`medium` are what the detector currently emits).
4. **`anomalies.detected` topic creation** — it is not in `kafka-init` in `docker-compose.yml`; confirm with M1/M2 who creates it explicitly rather than relying on auto-creation defaults (M1 already fixed one bug of exactly this kind in Phase 6).
5. **pgvector availability** — may require M1 to change the TimescaleDB image, or may be avoided entirely via the NumPy path.

---

## 2. Target layout

```
services/diagnosis-service/
|- PLAN.md                    this file
|- README.md                  what it does, how to run standalone (Phase 1)
|- Dockerfile
|- requirements.txt
|- config/
|   \- dependency_graph.yaml  Sock Shop topology from CONTRACTS.md
|- app/
|   |- main.py                FastAPI app, /health, /analyze
|   |- settings.py            env-var config (mirrors PG_*/KAFKA_BOOTSTRAP conventions)
|   |- models.py              Pydantic: AnomalyEvent, Candidate, Hypothesis, AnalyzeResponse
|   |- db.py                  psycopg2 connection + retry (copy metrics-sink's pattern)
|   |- consumer.py            anomalies.detected -> anomalies table
|   |- graph.py               dependency graph load + traversal
|   |- scoring.py             deterministic candidate scoring (NO LLM)
|   |- retrieval.py           embedding + similarity search over the incident corpus
|   |- prompts.py             prompt templates, context budgeting
|   |- llm.py                 Ollama client, JSON mode, validate-and-retry
|   |- guardrail.py           evidence-ID validation (the hallucination filter)
|   \- pipeline.py            orchestrates scoring -> retrieval -> llm -> guardrail
|- corpus/
|   |- incidents/*.md         50-150 past-incident writeups
|   \- ingest.py              embed + load corpus into the DB
|- fixtures/
|   |- anomalies/*.json       hand-written anomaly events (develop without M2)
|   \- expected/*.json        expected hypothesis shapes for tests
\- tests/
    |- test_scoring.py
    |- test_graph.py
    |- test_guardrail.py
    \- test_pipeline_fixtures.py
```

New SQL migration I own: `timescaledb/init/005_diagnosis.sql`.

**Caveat:** files in `timescaledb/init/` only execute on a *fresh* volume. Applying it to an
existing dev database means running it manually (Adminer on `localhost:8082`, or
`docker compose exec timescaledb psql -U postgres -d metrics -f ...`), or `docker compose down -v`
first. Document whichever is done, because M1 hit the same constraint with `004_evidence.sql`.

---

## 3. Phases

### Phase 0 — Environment proof (do this first; it can invalidate later phases)

Two scripts automate the checks. They are read-only and create nothing:

```
bash   services/diagnosis-service/scripts/phase0_stack_check.sh
python services/diagnosis-service/scripts/phase0_llm_bench.py
```

#### Host baseline (measured on this machine)

| Item | Value | Consequence |
| --- | --- | --- |
| GPU | RTX 3050 Ti Laptop, **4096 MiB** | Confirms the 4 GB ceiling. `phi4-mini` at Q4 (~2.5 GB) plus `nomic-embed-text` (~300 MB) plus an 8K context fits; a 7B-or-larger model does not. |
| System RAM | **15.2 GB** | M1's stack is 22 containers (Sock Shop + Kafka + TimescaleDB + Prometheus). Docker Desktop's WSL2 default is about half of RAM. Expect to need a `~/.wslconfig` with `memory=10GB` if the stack gets starved. |
| Free disk | **49 GB of 323 GB** | Models are roughly 3 GB, images 5-6 GB. Enough, but not roomy — do not also keep a second model family around. |
| Python | 3.12.0 | Fine. The benchmark script is stdlib-only so it runs before `requirements.txt` exists. |

#### Steps

1. **Start Docker Desktop**, then `docker compose up -d` from the repo root. Confirm M1's stack is
   healthy: 22 containers, zero restarts.
2. **Install Ollama** (`https://ollama.com/download`), then:
   ```
   ollama pull phi4-mini
   ollama pull nomic-embed-text
   ```
3. **Set `OLLAMA_HOST=0.0.0.0` as a Windows user environment variable and restart Ollama.**
   This is not optional and not cosmetic: on Windows, Ollama binds `127.0.0.1` by default, which a
   container cannot reach *even through* `host.docker.internal`. Without it, `diagnosis-service`
   gets connection-refused from inside Docker in Phase 1 and the cause is not obvious from the error.
   `phase0_stack_check.sh` check 6 tests exactly this.
4. **Run `phase0_stack_check.sh`.** It verifies: Docker up; container count and crash-loopers; the
   `metrics`, `deploys`, `evidence`, `fault_scenarios` tables exist and are non-empty; metric
   **freshness** (stale data looks like present data but silently breaks time-window queries); that
   my own Phase 2 tables do *not* exist yet; pgvector availability; the three Kafka topics; a real
   sample off `anomalies.detected`; and Ollama reachability from both the host and a container.
5. **Run `phase0_llm_bench.py`.** It reports generation speed, `/analyze`-shaped latency, GPU-versus-CPU
   placement, embedding dimensionality, and JSON validity over 10 runs on a prompt shaped like the real
   Phase 6 one. It also previews the Phase 7 guardrail by counting runs that cited evidence IDs never
   present in the prompt.
6. **Paste both scripts' output into `README.md`.** Unrecorded benchmarks are not evidence.

#### Things the scripts will likely flag, and what each means

- **`anomalies.detected` missing.** It is not created in `kafka-init` in `docker-compose.yml`, so it
  only springs into existence when M2 first publishes — with Kafka's default partitioning rather than
  the `3 partitions, keyed by service` the other topics use. M1 already fixed one bug of exactly this
  shape in Phase 6. This is open item 4; raise it rather than working around it.
- **`anomalies.detected` empty.** M2 has not detected anything. Inject a fault with M1's harness on
  port 5001 to generate real input.
- **`deploys` empty.** `deploy-emitter` fabricates one every two minutes; wait, or `POST /deploys` on
  port 5000.
- **pgvector unavailable.** Expected on the plain `timescale/timescaledb` image. Take the NumPy path
  in Phase 5; do not ask M1 to change the image for a 150-row corpus.
- **Embedding dimension not 768.** Update the `vector(768)` comment in the Phase 2 schema to match reality.

**Definition of done:** a written note in `README.md` stating — M1's stack runs; the upstream
tables and topics are reachable with the shapes CONTRACTS.md claims; `phi4-mini` benchmark numbers
(tok/s, latency, GPU placement, valid-JSON rate out of 10); pgvector available yes or no; and a
container can reach Ollama. Nothing is built yet, and that is correct.

#### Phase 0 outcome (2026-09-13) — status: DONE. Full numbers in `README.md`.

Findings that change later phases:

1. **pgvector is available.** Phase 2 uses `embedding vector(768)` instead of JSONB, and Phase 5
   uses the `<=>` operator. The NumPy path is no longer needed. Confirmed dimension: 768.
2. **`phi4-mini` passes on correctness, not on fit.** 10/10 valid JSON, 0/10 hallucinated IDs,
   about 5 s p50 at `num_ctx` 8192 — but 44% of it runs on CPU, because Windows holds about 800 MB
   of the 4 GB. A 4K context did not fix this. Decision: keep 8192.
3. **First model load can return HTTP 500** (runner crash `0xc0000409`, succeeds on retry). Phase 6's
   Ollama client must retry transport and 5xx errors separately from JSON-validation retries.
4. **M2's `evidence_window` is zero-width** — `start == end == t_onset` in every real event sampled.
   Phase 4 must not derive lookback ranges from it: use `t_onset - 30min` for deploys and a fixed
   `t_onset ± 2min` for co-anomaly overlap, and raise with M2 whether the window is meant to be wider.
5. **M2 does not deduplicate yet.** One real slowdown produced three anomalies within about 130 ms
   (`catalogue` p95, `catalogue` p99, `user` p95), each with its own `anomaly_id`. Until M2's Week 4
   grouping lands, Phase 4's co-anomaly signal will count these as independent corroboration, and
   M4 would request three analyses for one incident. Treat same-onset anomalies (within the ±2min
   window) as one incident when scoring; raise the grouping timeline with M2.
6. **`anomalies.detected` has 1 partition**, confirming open item 4: it is auto-created on first
   publish rather than by `kafka-init`. Harmless for correctness today; it is the bug shape M1 fixed
   in Phase 6, so it still belongs in `kafka-init`.
7. **The stack check script needed `MSYS_NO_PATHCONV=1`** — Git Bash rewrote `/opt/kafka/...` into a
   Windows path before it reached the container. Fixed in the script.

---

### Phase 1 — Skeleton service

- `requirements.txt`: `fastapi`, `uvicorn`, `pydantic`, `psycopg2-binary`, `kafka-python`, `httpx`, `pyyaml`, `numpy`, `pytest`.
- `settings.py` reading env vars with the repo's existing names and defaults: `KAFKA_BOOTSTRAP=kafka:9092`,
  `PG_HOST=timescaledb`, `PG_PORT=5432`, `PG_DB=metrics`, `PG_USER=postgres`, `PG_PASSWORD=Abcd1234#`,
  plus `OLLAMA_URL=http://host.docker.internal:11434`, `LLM_MODEL=phi4-mini`,
  `EMBED_MODEL=nomic-embed-text`, `LLM_CONTEXT_TOKENS=8192`.
- `models.py` — Pydantic models matching CONTRACTS.md **exactly**:
  - `AnomalyEvent`: `anomaly_id, services: list[str], metrics: list[str], severity, t_detected, t_onset, evidence_window: {start, end}`
  - `Hypothesis`: `rank: int, cause: str, confidence: float (0-1), evidence_ids: list[str], proposed_action: str`
  - `AnalyzeResponse`: `hypotheses: list[Hypothesis]`
  - `AnalyzeRequest`: `anomaly_id: str`
- `main.py` — `GET /health` returning `{"status":"ok"}`; `POST /analyze` returning a hardcoded
  contract-shaped response for now, so M4 is unblocked immediately.
- `Dockerfile` mirroring `services/metrics-sink/Dockerfile`, port **8000**.
- Add a `diagnosis-service` block to the root `docker-compose.yml`: `build:`, `ports: 8000:8000`,
  `depends_on: kafka-init (service_completed_successfully) + timescaledb (service_healthy)`,
  the env vars above, `networks: diagnosis-net`, and
  `extra_hosts: ["host.docker.internal:host-gateway"]` so the container can reach Ollama on the host.
- `README.md`: one paragraph on what it does plus how to run it standalone (the project's
  stated definition of done for each service).

**Definition of done:** `curl localhost:8000/health` returns ok, and `POST /analyze` with
`{"anomaly_id":"anom-0001"}` returns a response that validates against CONTRACTS.md's shape.
The container comes up as part of `docker compose up` with no manual steps.

---

### Phase 2 — Storage and fixtures

`timescaledb/init/005_diagnosis.sql`:

```sql
-- M3. Local copy of M2's pub/sub anomaly events, so POST /analyze can
-- resolve an anomaly_id. See PLAN.md section 1, "Anomaly lookup".
CREATE TABLE IF NOT EXISTS anomalies (
    anomaly_id      TEXT PRIMARY KEY,
    services        TEXT[] NOT NULL,
    metrics         TEXT[] NOT NULL,
    severity        TEXT NOT NULL,
    t_detected      TIMESTAMPTZ NOT NULL,
    t_onset         TIMESTAMPTZ NOT NULL,
    window_start    TIMESTAMPTZ NOT NULL,
    window_end      TIMESTAMPTZ NOT NULL,
    raw             JSONB NOT NULL,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS anomalies_detected_idx ON anomalies (t_detected DESC);

-- Past-incident corpus for retrieval. pgvector confirmed available in Phase 0.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS incidents (
    incident_id   TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    body          TEXT NOT NULL,
    services      TEXT[],
    fault_type    TEXT,
    source        TEXT,              -- 'public_postmortem' | 'synthetic'
    embedding     vector(768),       -- nomic-embed-text; dimension confirmed in Phase 0
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Every hypothesis ever returned: the audit trail M4 links to, and the
-- record the Week 8-9 evaluation scores against.
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
```

`pipeline_mode` and `model_version` exist from day one specifically so the Week 9 ablations are
a config flag, not a rewrite.

- `consumer.py`: background thread consuming `anomalies.detected` and upserting into `anomalies`.
  Copy the retry-until-Kafka-is-up pattern from `services/anomaly-detector/main.py`.
- `fixtures/anomalies/`: **8-10 hand-written anomaly events** covering the fault types M1's
  injector already produces (`bad_deploy_latency`, `service_crash`, `db_pool_saturation`) plus a
  multi-service cascade and one benign or ambiguous case. These are what Phases 3-6 are developed
  against — never block on M2.
- `POST /analyze` now loads the real anomaly from the DB and returns 404 on an unknown ID.

**Definition of done:** migration applied (state how); a real anomaly from M2 appears in the
`anomalies` table; fixtures load and validate against the `AnomalyEvent` model; `/analyze`
resolves a real `anomaly_id` and 404s an invented one.

---

### Phase 3 — Dependency graph (deterministic, no LLM)

`config/dependency_graph.yaml`, transcribed from CONTRACTS.md — transcribe it, do not invent it:

```yaml
edges:
  - [edge-router, front-end]
  - [front-end, catalogue]
  - [front-end, carts]
  - [front-end, orders]
  - [front-end, user]
  - [catalogue, catalogue-db]
  - [carts, carts-db]
  - [orders, orders-db]
  - [orders, payment]
  - [orders, shipping]
  - [orders, user]
  - [user, user-db]
  - [shipping, rabbitmq]
  - [queue-master, rabbitmq]
```

`graph.py` — plain dicts plus BFS; a graph library is not warranted for 14 nodes:

- `downstream(service, max_hops=3)` — services this one calls. **These are the root-cause candidates**:
  `front-end` looking slow is usually `catalogue` or `orders` actually being slow.
- `upstream(service, max_hops=3)` — services that call this one. **This is blast radius**: who is
  affected, and which *other* anomalies are likely symptoms of the same cause.
- `distance(a, b)` — hop count, `None` if unreachable.

**Definition of done:** `tests/test_graph.py` asserts that `downstream('front-end')` includes
`catalogue-db` at distance 2, `upstream('catalogue-db')` includes `edge-router` at distance 3, and
`rabbitmq` is not reachable from `catalogue`. All pass.

---

### Phase 4 — Candidate scoring (deterministic, no LLM) — review this diff carefully

The most important phase. Three independent signals, plain arithmetic, fully testable. This is
also the **baseline** the Week 9 ablation compares the LLM against.

For each candidate service (the anomalous services plus their 3-hop-or-less downstream set):

1. **Deploy proximity** — query `deploys` for that service within `t_onset - 30min` to `t_onset`.
   Score `exp(-minutes_before_onset / 10)`, so a deploy 3 minutes before scores about 0.74 and one
   25 minutes before about 0.08. No deploy scores 0. Keep the matched `deploy_id` for citation.
2. **Graph proximity** — `1 / (1 + distance_from_anomalous_service)`. The anomalous service itself
   scores 1.0, a direct dependency 0.5, two hops 0.33.
3. **Co-anomaly** — was this candidate *also* anomalous in the evidence window? Query `anomalies`
   for overlapping windows mentioning it. Binary 0 or 1 for now; this is the multi-anomaly handling.
   A downstream service that is also anomalous is a much stronger cause signal.
4. **Incident similarity** — filled in by Phase 5; score 0 until then.

```
score = 0.40*deploy_proximity + 0.25*graph_proximity + 0.20*co_anomaly + 0.15*incident_similarity
```

Weights live in `settings.py`, never hardcoded — Week 8 tunes them against real eval numbers.
Every candidate carries a per-signal breakdown, so any ranking can be explained without the LLM.

Write one `evidence` row per contributing signal (`category` of `anomaly`, `metrics`, `deployment`,
or `dependency`) with `relevance` set to that signal's normalised contribution. These `evidence_id`
values are what the LLM is allowed to cite, and what the Phase 7 guardrail checks against.

**Definition of done:** `tests/test_scoring.py` runs all fixtures with zero LLM calls and, for the
`bad_deploy_latency` fixture, ranks the deployed service first. A `GET /candidates/{anomaly_id}`
debug endpoint returns the ranked list with per-signal breakdowns.

---

### Phase 5 — Retrieval (RAG)

**Corpus, 50-150 records.** Two sources:

- Public postmortems from `github.com/danluu/post-mortems` — real incidents, and citable in the report.
- Synthetic writeups matching the exact fault types in `fault_scenarios` (`bad_deploy_latency`,
  `service_crash`, `db_pool_saturation`) written against Sock Shop service names, so retrieval has
  something genuinely relevant to find.

Each record carries: title, symptom description, root cause, resolution, affected services, fault type.

`corpus/ingest.py` — for each file: read, then `POST {OLLAMA_URL}/api/embeddings` with
`nomic-embed-text`, then store text plus the 768-dim vector in `incidents`. Idempotent and re-runnable.

`retrieval.py` — **hybrid**, not pure vector:

1. Structured pre-filter: incidents whose `services` intersect the anomalous services or their graph
   neighbours, **or** whose `fault_type` matches a suspected type.
2. Cosine similarity of the anomaly's query embedding against the filtered set (pgvector `<=>` if
   available, otherwise NumPy over the JSONB vectors — at this corpus size the difference is unmeasurable).
3. Return `top_k=3`; cap it, because of the context budget.

Query text is the anomaly summary plus affected services, affected metrics, and deviation magnitude.

The top similarity score feeds signal 4 of Phase 4's formula. Write one
`category='similar_incident'` evidence row per retrieved incident.

**Definition of done:** corpus ingested with a row count reported; for the `db_pool_saturation`
fixture, the top-3 retrieved incidents are manually judged relevant, with the judgement written into
`README.md` — that is the honest way to report retrieval quality before the Week 8 harness exists.
Pure-vector and hybrid results compared on at least two fixtures; hybrid is kept only if it is
actually better.

---

### Phase 6 — LLM reasoning

One call, tightly scoped. The LLM does **not** search, compute, or choose freely.

`prompts.py` builds a prompt containing only:

- The anomaly: services, metrics, severity, onset, observed versus baseline values.
- The **top 3-5 scored candidates** with their per-signal breakdowns and `evidence_id` values.
- The top-3 retrieved incidents — title, root cause, resolution, *truncated*, not full text.
- Deploy diffs (`commit_sha`, `config_diff`) for candidates that had a recent deploy.
- The exact allowed action vocabulary and the exact output schema.

**Context budgeting is mandatory**, not aspirational: count tokens before sending, and when over
`LLM_CONTEXT_TOKENS` minus a response reserve, drop in this order — incident bodies first (keeping
titles and root cause), then config diffs, then candidates beyond the top 3. Log every truncation.

`llm.py` — `POST {OLLAMA_URL}/api/chat` with `format: "json"` and
`options: {num_ctx: 8192, temperature: 0.1}`. Low temperature because ranking should be stable, and
stability matters for reproducible evaluation. Then **validate-and-retry**:

```
for attempt in 1..3:
    response = call_ollama(prompt)
    try: parse JSON, validate against Pydantic AnalyzeResponse -> return
    except: append the validation error to the prompt and retry
else: fall back to the deterministic Phase 4 ranking with a templated cause
      string, and mark the record accordingly
```

A small local model will fail schema compliance sometimes. The fallback means `/analyze` **always**
returns a valid contract-shaped response — M4's UI must never see a 500 because a model misbehaved.
Log every retry and fallback; those counts are a result worth reporting, not an embarrassment to hide.

The LLM's three jobs: write a grounded one-sentence `cause` per candidate; optionally re-rank; pick
one `proposed_action` from the enum with a real target ID.

**Definition of done:** all fixtures produce schema-valid hypotheses. Retry and fallback rates over
10 runs per fixture are recorded in `README.md`. Measured `/analyze` p50 and p95 latency recorded.
The prompt lives in a file, not an f-string buried in logic, because Week 8 iterates on it heavily.

---

### Phase 7 — Evidence guardrail — read this diff carefully, do not skim it

The hallucination filter. Pure set membership; nothing clever.

Before any hypothesis leaves the service:

1. Build the allowed set: every `evidence_id` written in Phases 4-5 for this anomaly, plus the
   `anomaly_id` itself, plus the `deploy_id` and `incident_id` values actually placed in the prompt.
2. For each hypothesis, every entry of `evidence_ids` must be in that set.
3. A hypothesis citing anything outside it is **dropped entirely** — not repaired, not shown with a
   warning, not passed to a human with a caveat. Dropped, and the rejection logged.
4. If every hypothesis is dropped, return the deterministic Phase 4 ranking instead.

This is the mechanism behind the 100% evidence-validity metric. It is an enforced filter, not a
measured hope.

**Definition of done:** `tests/test_guardrail.py` includes a deliberately poisoned LLM response
citing `ev-9999` and `dep-fake-001`, and asserts those hypotheses never appear in the output. A
counter of rejected hypotheses is exposed for the evaluation report.

---

### Phase 8 — Persistence, ablations, evaluation support

- Write every returned hypothesis to `hypotheses` with `model_version`, `pipeline_mode`, `latency_ms`.
- `pipeline_mode` switchable per request (`?mode=`) or by env var:
  - `full` — scoring, graph, retrieval, LLM. The real system.
  - `llm_only` — anomaly straight to the LLM, no scoring, retrieval, or graph. **Week 9 ablation.**
  - `no_graph` — scoring and retrieval, with the graph-proximity weight zeroed. **Optional second ablation.**
  - `deterministic` — Phase 4 only, no LLM. The honest baseline: does the LLM actually help?
- `GET /hypotheses/{anomaly_id}` for M4 and for M2's evaluation runner.
- Response caching keyed by `anomaly_id + mode + model_version`, because eval runs re-query the same
  anomalies repeatedly and local inference is slow.

**Definition of done:** the same fixture runs in all four modes and produces comparably shaped
output; results are queryable from SQL in one statement, which is what makes the Week 10
"regenerable from a single script" criterion achievable.

---

## 4. Mapping to the 10-week plan

| Week | Phases | Notes |
| --- | --- | --- |
| 1-2 | 0, 1 | Benchmark `phi4-mini` **before** designing around it. Give M4 a working `/analyze` stub immediately. |
| 3 | 2, 3, plus **start corpus and prompt work** | Prompting starts now, not Week 6 — it is the highest-variance work and needs calendar time, not effort. |
| 4 | 4, 5 | Deterministic scoring plus hybrid retrieval. Both fully testable without the LLM. |
| 5 | 6, 7 | Action vocabulary frozen with M4; guardrail in place. |
| 6 | swap fixtures for real data | Real `anomalies.detected`, real deploy log. Expect retuning: fixtures are cleaner than reality. |
| 7 | 8 | Persistence and ablation modes wired. Widen the corpus if Week 6 retrieval looked thin. |
| 8 | tuning | Tune weights, `top_k`, and the prompt against M2's first real eval numbers. |
| 9 | ablations | `full` versus `llm_only` (required), `no_graph` (optional), `deterministic` baseline. Report honestly even if unflattering. |
| 10 | report | Own Top-1/Top-3, MRR, evidence validity. Write up the reasoning architecture and the ablation findings. |

## 5. My metrics

| Metric | Definition | Target |
| --- | --- | --- |
| Root-cause accuracy | Top-1 / Top-3 hit rate versus `fault_scenarios.ground_truth_service` | Top-3 above 70% |
| Ranking quality | Mean Reciprocal Rank across scenarios | MRR above 0.6 |
| Evidence validity | Share of returned hypotheses citing only real evidence IDs | 100%, enforced in Phase 7 |
| Time to hypothesis | `/analyze` p50 and p95 | Report it; no target committed until Phase 0's benchmark |

## 6. Risks I own

| Risk | Mitigation |
| --- | --- |
| `phi4-mini` too weak for causal reasoning | Deterministic scoring does the hard part; the LLM only explains and re-ranks. `deterministic` mode quantifies exactly how much the LLM contributes. |
| Malformed or invalid JSON from a small model | Pydantic validation, 3-attempt retry, deterministic fallback. `/analyze` never returns a 500 for this reason. |
| Hallucinated evidence IDs | Phase 7 set-membership filter drops the hypothesis outright. |
| 4 GB VRAM insufficient even for 3.8B | Phase 0 benchmarks before any design depends on it. Fallbacks in order: smaller quantisation, then CPU offload (slower but still correct), then a hosted free-tier endpoint for development only, documented as a deviation from the self-hosted goal. |
| Corpus too small for useful retrieval | Public postmortems plus synthetic variants; report corpus size as an explicit experimental variable rather than hiding it. |
| pgvector unavailable in M1's image | NumPy brute force over 150 vectors or fewer. No image change needed, no blocking on M1. |
