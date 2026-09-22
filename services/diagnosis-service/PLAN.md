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
| Anomaly lookup | The shared `anomalies` table, **written by M2's detector** | Open item 1, now resolved. M3 built this table first and consumed `anomalies.detected` to fill it; M2 then persisted every event themselves, and PR #10 merged the two designs (M3's `raw` and `source`, M2's `detector`). M3's consumer and its copy of the table were removed on 2026-09-16; this service only reads the table, and writes fixture rows. |
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
8. **M2's detector floods on `memory_bytes`** — 140 of 157 anomalies over two hours with no fault
   injected, about 78 alerts/hour. Until fixed, M3 should expect most `/analyze` calls to be noise,
   and the co-anomaly signal is unreliable because many services look anomalous at once.

Findings 4, 5, 6 and 8 belong to other members and are recorded as GitHub issues rather than worked
around silently or fixed unilaterally:

| Finding | Issue | Owner |
| --- | --- | --- |
| 8 — `memory_bytes` false-positive flood | [#2](https://github.com/arghya0003/AI-Assisted-Incident-Diagnosis-System/issues/2) | M2 |
| 4 — zero-width `evidence_window` | [#3](https://github.com/arghya0003/AI-Assisted-Incident-Diagnosis-System/issues/3) | M2 |
| 5 — no anomaly grouping, `anomaly_id` collision risk | [#4](https://github.com/arghya0003/AI-Assisted-Incident-Diagnosis-System/issues/4) | M2 |
| 6 — `anomalies.detected` missing from `kafka-init` | [#5](https://github.com/arghya0003/AI-Assisted-Incident-Diagnosis-System/issues/5) | M1 |

M3's workarounds for 4 and 5 (derive windows from `t_onset`; group same-onset anomalies when scoring)
are temporary and should be removed once the corresponding issue is closed.

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

#### Phase 1 outcome (2026-09-13) — status: DONE

Verified against the running stack:

| Check | Result |
| --- | --- |
| `docker compose up -d --build diagnosis-service` | builds and starts after `kafka-init` completes and `timescaledb` is healthy |
| Container health / restarts | `healthy` / 0 |
| `GET /health` | 200 `{"status":"ok",...,"pipeline_mode":"stub"}` |
| `POST /analyze` (real M2 anomaly id) | 200, contract shape, header `X-Diagnosis-Mode: stub` |
| `POST /analyze` with `{}` | 422 |
| Ollama from inside the container | reachable via `host.docker.internal` (version 0.34.0) |
| `python -m pytest` | 32 passed |

Decisions made while building, beyond what this section specified:

- **The stub is deliberately honest:** `cause` starts with `[stub]`, `confidence` is `0.0`,
  `proposed_action` is `no_action`, and it cites only the requested `anomaly_id`. A contract-valid
  stub that looked like a real rollback recommendation would be indistinguishable from a real
  diagnosis in M4's UI.
- **The models enforce more than the contract text states,** because Phase 6 will validate LLM
  output with the same classes: `proposed_action` must match the four-value vocabulary exactly;
  `confidence` must be within 0–1; `evidence_ids` must be non-empty and non-blank; ranks must be
  exactly 1..n; and extra fields are rejected on responses and requests. Unknown fields on
  `AnomalyEvent` are *ignored*, because M2 owns that shape.
- **Dependencies are added per phase, not all up front.** `requirements.txt` holds only what the
  running service imports (FastAPI, uvicorn, Pydantic); `pytest`/`httpx` are in
  `requirements-dev.txt`, so the image doesn't ship test tooling. `psycopg2`, `kafka-python-ng`,
  `pyyaml` and `numpy` arrive with Phases 2, 3 and 5.
- **Not verified:** a full `docker compose down -v && up` from a clean checkout. That destroys
  M1's data volume, so it waits for an agreed team-wide rebuild rather than being run unilaterally.

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

#### Phase 2 outcome (2026-09-13) — status: DONE

| Check | Result |
| --- | --- |
| Migration | Applied to the existing dev volume with `docker compose exec -T timescaledb psql ... -f /docker-entrypoint-initdb.d/005_diagnosis.sql` (the command is in the SQL header and README). Created `anomalies`, `incidents`, `hypotheses`, plus pgvector 0.7.2. |
| Real M2 anomalies stored | On first start, the consumer backfilled 184 events from the topic with 0 collisions and 0 malformed. After that, a new event was stored 2 ms after `t_detected`. |
| Fixtures | 10 files load, validate against `AnomalyEvent`, and are stored with `source='fixture'` |
| `/analyze` real id (`anom-1789296738161`) | 200, stub hypothesis naming `catalogue (latency_p95_ms)` |
| `/analyze` fixture id (`anom-fx-08`) | 200 |
| `/analyze` invented id | 404 |
| Container | healthy, 0 restarts; `/health` reports `database: ok`, `consumer: running` |
| Tests | 78 passed inside the compose network (`scripts/test_in_docker.sh`), including 12 real-database tests |

Decisions made while building, beyond what this section specified:

- **`anomalies.source` column (`kafka` or `fixture`).** Fixtures live in the same table as real
  events so `/analyze` works on them, so evaluation needs a way to exclude them. Fixture ids also
  start with `anom-fx-` so they can't clash with M2's `anom-<epoch ms>` ids.
- **Index on `t_onset`, not `t_detected`.** Phase 4's co-anomaly lookup searches by onset.
- **Never overwrite a stored anomaly.** A redelivered event is a `duplicate`, which is expected
  with at-least-once Kafka. The same id with different content is a logged `collision`, and the
  first event is kept (issue #4).
- **Malformed events are logged and skipped, not retried.** Retrying would fail identically and
  block every later anomaly on that partition.
- **Kafka offsets are committed only after the database commit**, so a crash re-reads events
  rather than losing them.
- **`/health` stays 200 when the database is down** and reports it in a `database` field. A
  restart can't fix a database outage, so failing the healthcheck would only add restart noise.
  `/analyze` returns 503 in that case.
- **Fixtures carry a `_fixture` block** with the true root cause, fault type and notes, so
  Phases 3–6 can assert correctness rather than just shape. With that block removed, each file
  is exactly M2's wire shape. The set deliberately includes a case where the root cause is not in
  `services` (a crashed container stops exporting metrics), and a pair with an identical symptom
  but different causes (`anom-fx-04` and `anom-fx-06`).
- **The API opens a connection per request instead of using a pool.** `/analyze` runs at human
  pace and will spend seconds in the LLM.

Found on this machine: a **Windows PostgreSQL 18 service listens on port 5432**, so from the host
`localhost:5432` is that server, not Docker's TimescaleDB. Services inside Docker are unaffected.
Host-side database tests skip instead of failing, and `scripts/test_in_docker.sh` runs them
inside the network. Stopping that Windows service is the owner's decision, so it was left running.

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

#### Phase 3 outcome (2026-09-13) — status: DONE

| Check | Result |
| --- | --- |
| Definition-of-done assertions | All three pass: `catalogue-db` is 2 hops below `front-end`; `edge-router` is 3 hops above `catalogue-db`; `rabbitmq` is unreachable from `catalogue`. |
| YAML equals the contract | A test holds its own copy of CONTRACTS.md's 14 edges and fails if the YAML drifts |
| True cause is always a candidate | For all 8 fixtures that have a ground truth, the true cause is an anomalous service or within 3 hops downstream of one. This includes `anom-fx-04/05/06`, where it is absent from `services`. |
| Real data | All 7 services M2 has raised anomalies on are graph nodes |
| Container | loads the graph at startup (`14 nodes, 14 edges`); healthy, 0 restarts |
| Tests | 114 passed inside the compose network |

Decisions made while building, beyond what this section specified:

- **Node kinds** (`gateway`, `service`, `datastore`, `broker`), transcribed from CONTRACTS.md's
  annotations (Traefik, MySQL, Mongo). Declaring nodes also lets the loader reject an edge with a
  misspelled service name. Phase 4 can use the kinds to decide which `proposed_action` targets
  make sense.
- **`distance(a, b)` is directed** (caller to callee), matching the definition of done, where
  `rabbitmq` is unreachable from `catalogue` even though an undirected path exists.
- **Unknown service names raise `UnknownService`** rather than returning an empty result, so
  Phase 4 must decide explicitly what to do with a service M2 reports that isn't in the graph.
- **Traversal order is deterministic** (breadth-first, sorted neighbours), so candidate lists
  and their evidence are reproducible run to run.
- **The graph loads at import time** in `main.py`, so a broken YAML stops the container at
  startup instead of failing the first `/analyze`.

Caveat for the team: in `docs/phase0-decisions.md`, M1 confirmed most edges from the images'
config, but `orders -> payment`, `orders -> shipping` and `orders -> user` are taken from the
standard Sock Shop reference architecture. They are the least verified edges, and `anom-fx-05`
and `anom-fx-08` depend on them.

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

#### Phase 4 outcome (2026-09-13) — status: DONE (fixtures); live check inconclusive, see finding 2

| Check | Result |
| --- | --- |
| All fixtures scored with zero LLM calls | A test blocks every socket connection while scoring all 10 fixtures |
| `bad_deploy_latency` fixtures rank the deployed service first | 4 of 4 (`anom-fx-01/02/03/08`), each citing the injected deploy, despite 2–3 background deploys per fixture |
| Other fixtures | `anom-fx-07` (DB saturation) is correct; benign `anom-fx-09` tops out at 0.45, against 0.84 for a real bad deploy; ambiguous `anom-fx-10` is an exact tie |
| Known misses (strict `xfail` tests, which fail loudly if they start passing) | Crashes `anom-fx-04/05/06`; `anom-fx-07` with a front-end deploy 3 min before onset |
| `GET /candidates/{id}` on the live stack | 200 with per-signal breakdown and evidence for real and fixture anomalies; 404 for unknown ids |
| Tests | 186 passed, 4 xfailed, inside the compose network |

Decisions made while building, beyond what this section specified:

- **`co_anomaly` means "deepest anomalous service"**: the candidate is anomalous (in this event,
  or in a related anomaly within ±120 s of onset), and nothing it calls is also anomalous. The
  plain "was it also anomalous" reading tied cause and symptom in every grouped fixture:
  `orders` and `front-end` both scored 1 in `anom-fx-02`. An anomalous service with anomalous
  dependencies is more likely a symptom.
- **Related anomalies only mark services; they never add candidates.** M2's `memory_bytes` flood
  (issue #2) would otherwise add unrelated services to almost every analysis. Related anomalies
  are matched from the same `source`, so fixtures and real events never mix.
- **Deploys after onset are excluded**, and a service's most recent deploy in the window is the
  one scored and cited.
- **Ties break by distance, then service name**, so rankings are reproducible.
- **Evidence is built but not yet written to the `evidence` table.** Each item has a
  deterministic id, `ev:<anomaly_id>:<category>:<source_id>`, and `/candidates` returns the list.
  Writing moves to Phase 6, when `/analyze` first cites evidence: a debug `GET` should not write,
  and a write path with no reader would go untested. There is no `metrics` evidence yet, because
  no signal reads metric values.
- **Weights are validated at startup** (non-negative, summing to 1), so a bad
  `SCORE_WEIGHT_*` value stops the container instead of skewing every score.
- **Fixture contexts** (`_fixture.context`) hold each scenario's deploys, including background
  ones, and related anomalies. The loader stores the related anomalies but not the deploys,
  because `deploys` is M1's table. So `/candidates` on a fixture id has no deploy signal, and can
  rank differently from `tests/test_scoring.py`.

Findings:

1. **deploy-emitter fabricates a deploy every 2.0 minutes** (102 in 3 h 22 min), so almost every
   candidate has a deploy inside the 30-minute lookback. On a real catalogue latency anomaly,
   the top candidate got a deploy score of 0.41 from a routine background deploy. Injected
   deploys land seconds before onset (score about 1.0) and still stand out. But a background
   deploy to a *symptom* service within about 7 minutes of onset outranks a no-deploy cause
   (the `anom-fx-07` xfail). This is Week 8 weight tuning. Asked M1 whether this deploy rate is
   intended for evaluation runs:
   [#7](https://github.com/arghya0003/AI-Assisted-Incident-Diagnosis-System/issues/7).
2. **The live bad-deploy injection produced no symptom.** `bad_deploy_latency` on `catalogue`
   (60 s at 5% CPU, scenario `scn-bad-deploy-latency-1789299649`) recorded its deploy
   (`dep-2026-09-13-0107`), but catalogue p95 stayed at 4.8 ms at about 0.2 requests/s, and M2
   raised nothing. The testbed carries too little traffic for a CPU throttle to matter. Over
   the preceding 3 hours:
   - every service with latency data ran at exactly 0.20 requests/s;
   - p95 took only 2–3 distinct values per service (histogram-bucket steps, not a live signal);
   - only `catalogue`, `payment` and `user` reported p95 at all;
   - no load-generator container was running (M1 dropped `user-sim` in Phase 0).

   So end-to-end ranking on live data is
   **not verified yet**, and the fixture tests are the evidence. Raised with M1 as
   [#6](https://github.com/arghya0003/AI-Assisted-Incident-Diagnosis-System/issues/6), because
   detection and diagnosis both depend on faults being visible.
3. **The memory flood reaches the co-anomaly set.** That real catalogue anomaly had 5 related
   anomalies within ±2 min (carts, orders and shipping `memory_bytes`). They add no candidates,
   but they can mark a downstream service co-anomalous (issue #2).
4. **Crash faults need a missing-metrics signal.** A stopped container stops reporting, so it is
   never anomalous and never has a deploy, and scoring cannot rank it (`anom-fx-04/05/06`). A
   "candidate stopped reporting metrics near onset" signal from M1's `metrics` table would
   address this. It is not in this plan's weights, so it is proposed here rather than built.
   **Resolved on 2026-09-16 by M2, not by M3:** their staleness detector now emits a `liveness`
   anomaly naming the silent service, which makes it anomalous, so the existing weights rank it.
   See "M2 detector upgrade" below.
5. **Open item for the team: what do hypotheses cite?** The CONTRACTS.md example cites source ids
   (`anom-0001`, `dep-...`, `incident-0042`), while `docs/evidence-model.md` says `/analyze`
   should return `evidence_id` values. Phase 6 needs one answer.

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

#### Phase 5 outcome (2026-09-13) — status: DONE (retrieval quality is modest; see the findings)

| Check | Result |
| --- | --- |
| Corpus ingested | 61 incidents (35 synthetic, 26 public postmortems), all with 768-dim embeddings; re-running updates in place |
| `db_pool_saturation` fixture (`anom-fx-07`), top 3 judged by hand | **2 relevant, 1 partly relevant.** Full judgement in `README.md` |
| Pure vector vs hybrid | Compared on all 10 fixtures. They differ on one (`anom-fx-03`), where hybrid swaps an irrelevant incident about a different service for an in-scope one. Hybrid is kept, but only marginally better |
| Retrieval inside `GET /candidates` | `retrieval_status: ok`; about 0.1 s per request including the embedding call; the matching candidate gets `incident_similarity` plus `similar_incident` evidence |
| Ollama down | `/candidates` still returns 200 with `retrieval_status: embedding_unavailable` and similarity 0 (tested) |
| Tests | 260 passed, 4 xfailed, inside the compose network |

Decisions made while building, beyond what this section specified:

- **Only the Symptoms section is embedded.** This is the most important change in the phase.
  The first run embedded title plus full body and was poor: `anom-fx-07` retrieved no
  connection-pool incident at all, and two short generic write-ups filled 14–16 of the 30 top-3
  slots across fixtures. A controlled comparison (3 document forms × 2 query phrasings × 2 modes,
  scored against the fixtures' known causes) picked symptoms-only by a wide margin:

  | Documents (hybrid) | Exact cause in top 3 | Right fault type in top 3 | Generic "hub" incidents in top 3 |
  | --- | --- | --- | --- |
  | Title + full body | 4/10 | 13/30 | 14/30 |
  | Title + symptoms | 5/10 | 10/30 | 11/30 |
  | **Symptoms only** | **5/10** | **18/30** | **3/30** |

  The reason: an anomaly query can only describe symptoms, while root-cause and resolution text
  pulls a document towards things the query can never mention. Rephrasing the query did not
  help. The full body is still stored, for the Phase 6 prompt.
- **`incident_similarity` is per candidate**: the best similarity among retrieved incidents whose
  root cause was in that service. The plan fed one "top similarity" into signal 4, but a value
  shared by every candidate adds the same amount to each and can never change the ranking.
- **Incident `services` names the root-cause service only**, never services that merely showed
  symptoms, so an incident can't boost a symptom.
- **Public postmortems name no Sock Shop services.** They pass the hybrid filter only by fault
  type and never boost a candidate. The public list had no licence, so entries are paraphrases
  that add nothing beyond the source summary and link the original.
- **The corpus uses a wider fault-type vocabulary** than the injector's three (adding
  `bad_deploy_errors`, `db_contention`, `capacity`, `config_error`, `dependency_failure` and
  `benign`), so real postmortems are labelled honestly rather than forced into three boxes.
- **Hybrid filter**: the candidate services (same set scoring uses) OR a fault type consistent
  with the anomaly's metrics, via a deliberately generous lookup table in `app/retrieval.py`.
- **nomic-embed-text task prefixes** (`search_query:` / `search_document:`) are used, as the model
  requires.
- **Retrieval never fails a request.** If Ollama is down, scoring runs without it and the
  response says so; the deterministic baseline must not depend on the LLM host.
- **The Ollama client retries transport errors and 5xx, but not 4xx**, per Phase 0 finding 3.
  Phase 6 reuses it for generation.

Findings:

1. **Retrieval is only as informative as the anomaly.** M2's event carries service, metric and
   severity, with no value, baseline or direction. An error-rate anomaly on `front-end` looks the
   same whatever caused it, so the crash fixtures (`anom-fx-04/05/06`) retrieve generic crash
   incidents for *other* services. Retrieval cannot fix the Phase 4 crash limitation.
2. **Similarity scores are compressed** (roughly 0.63–0.77 for everything retrieved), so
   `incident_similarity` contributes about 0.10–0.12 to any candidate an incident names, relevant
   or not. It is a weak signal; Week 8 should consider rescaling it or lowering its weight.
3. **Results are optimistic.** The synthetic incidents and the fixtures were written by the same
   person from the same fault types, and the symptoms-only choice was selected on those fixtures.
   No public postmortem reached a fixture's top 3. Week 8 should measure retrieval on real
   injected faults or on held-out incidents, which depends on #6 (no standing traffic) being
   resolved.

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

#### Phase 6 outcome (2026-09-14) — status: DONE against the definition of done; the cause text is not yet trustworthy (finding 1)

| Check | Result |
| --- | --- |
| Every fixture produces schema-valid hypotheses | **100/100** runs (10 fixtures × 10 runs) contract-valid |
| Retry and fallback rates | **0/100** needed a retry, **0/100** fell back. One Ollama runner crash (HTTP 500, `0xc0000409`) on the warm-up call was recovered by the client's retry. |
| `/analyze` latency | Live over HTTP, 10 calls: **p50 12.1 s, p95 14.0 s**. In-process pipeline with fixture context, 100 runs: p50 14.1 s, p95 17.0 s, max 19.5 s. |
| Prompt in files | `app/prompts/analyze_system.txt`, `analyze_user.txt`, `analyze_retry.txt` |
| Context budget estimate | Ollama's prompt-token counts were 0.71–0.76 of the estimate, so the budget errs safe as intended |
| Always contract-valid | Tested for every fixture with an unreachable model and with a model that returns prose |
| Tests | 327 passed, 4 xfailed, inside the compose network |

Quality, from the same 100 runs (beyond the definition of done):

| Measure | Result |
| --- | --- |
| Rank-1 service is the true root cause | **50/80**, identical to the deterministic scorer's rank 1 (50/80). The LLM kept the scorer's top candidate in 90/100 runs. |
| Bad-deploy fixtures (`anom-fx-01/02/03/08`) | 40/40: correct service, and rollback of the injected deploy |
| Benign and ambiguous fixtures (`anom-fx-09/10`) | 20/20 `no_action` |
| Harmful action | `anom-fx-06` (carts crash): rolled back orders' routine deploy from 4 minutes before onset, 10/10 |
| Replies normalised | 35/100 (a repeated candidate dropped, or hypotheses reordered by confidence) |
| Run-to-run variation | None of substance at temperature 0.1: each fixture's 10 runs gave the same rank-1 service and action |

Decisions made while building, beyond what this section specified:

- **Ollama's JSON-schema output instead of `format: "json"`.** Measured at no latency cost, and
  enums are enforced during decoding. With plain JSON mode, a prompt that pushed `ev-9999` and
  `dep-fake-001` got both emitted verbatim.
- **Each hypothesis is bound to one candidate in the schema**, through an internal `service` field
  and one schema variant per candidate listing only that candidate's citable ids and actions.
  The first version had flat lists of allowed ids and actions, plus rules in the prompt.
  phi4-mini proposed rolling back front-end's deploy as the fix for catalogue, proposed
  `restart_service` for every fixture including the benign ones, and ignored rules added to stop
  it. `service` is stripped before the response, so the contract is unchanged.
- **Only `no_action` and `rollback_deploy` are offered.** A rollback is offered only for the
  candidate's own deploy with deploy score >= 0.5 (within about 7 minutes). Restart and scale are
  never offered, because no signal shows a service has failed or is overloaded. The fallback uses
  the same rule (`app/hypotheses.py`).
- **Output bounds**: `num_predict` 768, at most 6 citations, cause at most 400 characters. On a
  poisoned prompt, schema-constrained decoding first ran for over 5 minutes; with a token cap it
  repeated one allowed id until the cap (42 s); with bounded arrays it finished normally in 10 s.
  Read timeouts are no longer retried, so a stuck generation can't cost three timeouts.
- **Two cosmetic reply problems are normalised rather than retried** (a repeated candidate, or rank
  disagreeing with confidence). Retrying costs about 12 s, and the model tends to repeat itself at
  temperature 0.1. Each normalisation is recorded and counted.
- **Hypotheses cite source ids** (anomaly, deploy, incident), as in the CONTRACTS.md example, not
  `ev:` evidence ids. Phase 4 open item 5 is still for the team to settle.
- **Fallback causes start with `Deterministic ranking (LLM not used):`**, and the
  `X-Diagnosis-Mode` header says which path answered.
- **Token estimate** is 3 characters per token; measured 3.45, and confirmed safe above.

Findings:

1. **The cause text still invents facts.** The schema fixed ids and actions, not prose. On live
   `/analyze` calls for fixtures (whose deploys are not in M1's `deploys` table), `anom-fx-01`
   said "catalogue's recent deployment lowered its CPU limit"; `anom-fx-07` claimed recent
   deploys to catalogue, front-end and catalogue-db; and `anom-fx-10` said "carts release enabled
   debug logging". Each was copied from a similar past incident (0008, 0022, 0006) and stated as
   current fact, despite rule 4. Two causes were just "Anom-fx-10". The Phase 7 citation
   guardrail cannot catch this. Options to decide before evaluation:
   - drop incident root-cause and resolution text from the prompt, keeping ids, titles and fault
     types;
   - reject in code a cause that mentions a deploy for a candidate that has none;
   - generate the cause text deterministically, leaving the LLM only ranking and confidence.
2. **The LLM adds no ranking accuracy on these fixtures** (50/80, the same as the scorer). Its
   value would have to be explanation, which finding 1 makes unreliable. Phase 8's
   `deterministic` versus `full` ablation should measure this on real injected faults.
3. **Background deploys now cause a harmful action.** For `anom-fx-06`, a routine orders deploy
   4 minutes before onset was offered for rollback and chosen. This is Phase 4 finding 1
   ([#7](https://github.com/arghya0003/AI-Assisted-Incident-Diagnosis-System/issues/7)) reaching
   the proposed action.
4. **Latency is 12–17 s**, against 5 s in Phase 0, because of a longer prompt and up to three
   hypotheses at about 25 tokens/s. That's acceptable for a human-approval workflow, but
   evaluation runs will need Phase 8's response cache.
5. **Ten runs per fixture measure stability, not spread**: at temperature 0.1 the output barely
   varies.

#### Phase 6 follow-up (2026-09-14): fixes A and B for finding 1

Chosen by the team member from the three options in finding 1:

- **A. Past incidents appear in the prompt without their root-cause or resolution text.** Only the
  id, title, fault type, root-cause service and similarity remain. The text is still stored and
  shown in `GET /candidates`. This removes the plan's first truncation step, so the budget now drops
  config diffs first, then candidates beyond the top 3.
- **B. A cause that asserts a deploy for a candidate with no recent deploy is rejected**, and the
  reply is retried with the reason (`claims_a_deploy` in `app/llm.py`). It matches deploy, release,
  rollout and upgrade wording, but not negated forms such as "no recent deploy". Its test cases
  include the invented causes phi4-mini actually wrote.

Live `POST /analyze` on the four fixtures whose causes had invented deploys (fixture deploys are
not in the database, so no candidate has one):

| Fixture | Before A and B | After A and B |
| --- | --- | --- |
| `anom-fx-01` | "catalogue's recent deployment lowered its CPU limit" | B rejected attempt 1; attempt 2 cites only the related anomaly |
| `anom-fx-06` | Orders "resembles incident-0009 where a missing configuration variable caused errors" | B rejected attempt 1; attempt 2: "1 hop downstream of the anomalous front-end, despite no recent deploy listed" |
| `anom-fx-07` | Claimed recent deploys to catalogue, front-end and catalogue-db | B rejected attempt 1; attempt 2 states only listed facts |
| `anom-fx-10` | "carts release enabled debug logging on the hot path" | Valid first time; no copied incident story |

What A and B do not fix:
- **Speed.** Retries raised those calls from about 12 s to 24–41 s.
- **Non-deploy slips remain.** `anom-fx-07` said front-end has "nothing it calls anomalous", but
  it calls catalogue, which is anomalous. Such errors are less likely to push an operator toward a
  wrong action than an invented deploy, but causes are still not fully trustworthy.

Evaluation after A and B (same 100-run harness with fixture context):

| Measure | Before A and B | After A and B |
| --- | --- | --- |
| Contract-valid responses | 100/100 | 100/100 |
| Valid on the first attempt | 100 | 56 |
| Needed a retry | 0 | 32 |
| Deterministic fallback | 0 | 12 (`anom-fx-01` ×2, `anom-fx-05` ×4, `anom-fx-07` ×6) |
| Latency p50 / p95 | 14.1 s / 17.0 s | 15.6 s / 40.2 s |
| Rank-1 service is the true root cause | 50/80 | 50/80 |
| Rank-1 rollbacks | 50, of which 10 wrong (`anom-fx-06`) | 54, of which 14 wrong: `anom-fx-06` ×10, plus `anom-fx-05` ×4, where the fallback rolled back a routine shipping deploy from 2 minutes before onset |
| Rejected attempts | 0 | 77, all from check B |

What those rejections were, from a diagnostic re-run of `anom-fx-05/07/08/09` printing every attempt:

- **Genuine invented deploys.** For `anom-fx-07`, the catalogue cause "catalogue deploy lowers its
  CPU limit and throttles the service" is incident-0008's title, word for word. Fix A kept titles,
  and titles carry the same stories.
- **Narration of a past incident rather than a claim about now**, such as "resembles a past
  incident where a front-end release disabled template caching" (`anom-fx-05`) and "Similar past
  incident incident-0003 suggests a bad deploy latency" (`anom-fx-08`). Rule 4 allows
  resemblance, but these still read as if a deploy were involved; `anom-fx-09`'s even described a
  shipping incident as evidence about rabbitmq. So they aren't clear-cut false positives.
- **Retries rarely change the reply.** At temperature 0.1, `anom-fx-05` and `anom-fx-07` returned
  the identical rejected sentence on all three attempts, so most retries only add about 12 s each
  before the fallback.
- **The token-budget estimate still holds.** Actual/estimated prompt tokens reached 1.30, but only
  because retry turns (the previous reply plus the rejection) are appended to the conversation,
  while the estimate covers the first attempt. First attempts stayed at about 0.74.

Net effect: no rejected cause reaches M4, so invented deploy claims are gone from responses. The
cost is 12% fallbacks, a 40 s p95, and four more wrong rollbacks through the fallback. Options for
the team member to choose from:

1. Drop incident titles from the prompt as well, leaving id, fault type, root-cause service and
   similarity.
2. When check B rejects a hypothesis, drop only that hypothesis instead of retrying the whole reply,
   and fall back only if none survive. This is faster, but for `anom-fx-07` it would drop the
   correct top candidate.
3. Retry at a higher temperature, so a retry can actually produce something different.
4. Make the fallback always propose `no_action`. That removes its wrong rollbacks, but also its
   correct ones.

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

#### Phase 7 outcome (2026-09-14) — status: DONE

| Check | Result |
| --- | --- |
| Poisoned LLM reply (`ev-9999`, `dep-fake-001`) | `tests/test_guardrail.py` runs it through the pipeline for all 10 fixtures. Neither id ever reaches a response. A partly poisoned reply keeps only its clean hypothesis; a fully poisoned one returns the deterministic ranking. |
| Validation alone would not catch it | A test shows the poisoned reply passes `validate_reply`, because its service and action are legitimate. The guardrail is the layer that enforces citations. |
| A hypothesis is dropped, not repaired | A hypothesis with one unknown id among valid ones is removed entirely, not returned with the bad id stripped |
| Deterministic ranking always passes | 0 rejections for every fixture |
| Counter exposed | `GET /stats` (checked, rejected, and fully-rejected counts, split into LLM and deterministic); `X-Guardrail-Rejected` header per response; a `guardrail_rejections` list on every pipeline result; and a total in `scripts/eval_llm.py` |
| Live `POST /analyze` (`anom-fx-01/07/09`) | 200, `X-Guardrail-Rejected: 0`; `/stats` showed 7 LLM hypotheses checked, 0 rejected |
| Real phi4-mini, 1 run per fixture | 10/10 contract-valid, **0 hypotheses dropped by the guardrail** |
| Tests | 378 passed, 4 xfailed, inside the compose network |

Decisions made while building, beyond what this section specified:

- **Allowed set**: the anomaly id, every evidence id scoring produced, and every id placed in the
  prompt (the anomaly, related anomalies, deploys and incidents across all shown candidates). When
  no prompt was built, the report's own citable source ids take the prompt's place. A deploy that
  exists but was never shown to the LLM is not citable.
- **The check is global, not per candidate.** Citing another candidate's real deploy is not a
  hallucination, and the contract only requires that ids resolve to real records. The
  per-candidate binding is already enforced earlier, by the response schema.
- **Every response passes the guardrail, including the deterministic fallback.** A fallback
  rejection is logged as a bug rather than silently allowed.
- **Surviving hypotheses are re-ranked 1..n**, as the contract requires; nothing else about them
  is changed.
- **Counters are in memory** and reset on restart. Phase 8's `hypotheses` table will make durable
  counts possible.

Findings:

1. **On phi4-mini with Ollama, the guardrail never fires** (0 of 10 real runs, and 0 live),
   because the response schema already limits evidence ids during decoding. It is a verified
   backstop, not an active filter. It becomes the active one if the service switches to a provider
   that doesn't enforce JSON schemas, which the instructor has allowed as an option.
2. **This one run repeated Phase 6's picture**: 40% of runs needed a retry, 1 fell back, and p95
   was 41 s. The AI's rank 1 matched the true cause in 4/8, against the scorer's 5/8: for
   `anom-fx-07`, check B's retry left a single front-end hypothesis. One run is too few to
   conclude from, but it reinforces Phase 6 finding 2.

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

#### Phase 8 outcome (2026-09-14) — status: DONE

| Check | Result |
| --- | --- |
| Same fixture in all four modes, comparably shaped output | All 10 fixtures ran in `full`, `no_graph`, `llm_only` and `deterministic`: 40/40 contract-valid, all stored. A unit test does the same for every fixture. |
| Results queryable from SQL in one statement | One `analyses` ⟕ `hypotheses` query returns all 40 runs with rank-1 service, action, confidence, attempts and latency (query in `README.md`) |
| Every returned hypothesis written with `model_version`, `pipeline_mode`, `latency_ms` | Yes, plus `analysis_id` and `service`. Every run gets an `analyses` row, including `llm_failed` runs with no hypotheses. The scorer's evidence is upserted into M1's `evidence` table. |
| Mode per request or by env var | `?mode=` and `PIPELINE_MODE`; an unknown mode returns 422 |
| `GET /hypotheses/{anomaly_id}` | Live: two stored runs newest first, each with hypotheses and service; 404 for an unknown anomaly |
| Response cache | Live, on a real anomaly: miss, then hit with the same `X-Analysis-Id`, then `?refresh=true` gave a miss and a new run |
| Tests | 427 passed, 4 xfailed, inside the compose network |

Ablation, 1 run per fixture per mode, with fixture context:

| Mode | Rank-1 = true cause (8 labelled) | Answered by | Retried | p50 / p95 | Rank-1 rollbacks (wrong) |
| --- | --- | --- | --- | --- | --- |
| `deterministic` | 5/8 | scorer 10 | — | 52 ms / 77 ms | 6 (2) |
| `full` | 4/8 | LLM 8, fallback 2 | 5 | 15.0 s / 40.7 s | 6 (2) |
| `no_graph` | 5/8 | LLM 9, fallback 1 | 3 | 13.1 s / 38.3 s | 6 (2) |
| `llm_only` | 3/8 | LLM 10 | 2 | 11.9 s / 26.1 s | 7 (4) |

Decisions made while building, beyond what this section specified:

- **An `analyses` table (migration `006_diagnosis_analyses.sql`) as well as `hypotheses`.** A run
  with no hypotheses (an `llm_only` failure) still needs a row, so failures, fallbacks and latency
  can be counted in SQL.
- **The cache key adds a configuration fingerprint** to anomaly, mode and model version. The
  fingerprint is a hash of the service version, LLM settings, scoring weights, pipeline settings
  and every prompt file, so an edited prompt never serves an old answer.
- **Only intended answers are cached.** A fallback or `llm_failed` run is never reused, so the next
  request makes a fresh attempt.
- **A run made before the anomaly's co-anomaly window closed is never reused**, because related
  anomalies may have arrived since. `?refresh=true` bypasses the cache. A failed database write
  still returns the answer, with `X-Persisted: false`.
- **`llm_only` gives the LLM the anomaly, related anomalies and raw deploys**, with every service as
  a possible cause and no scores, graph or retrieved incidents. It keeps the same per-service limits
  (cite its own records; roll back its own deploys within about 7 minutes). It has **no fallback**:
  a failure is an empty answer, so the ablation measures the LLM alone.
- **`no_graph` zeroes the graph weight and rescales the other weights to sum to 1**, as the plan
  said. The graph still defines the candidate set, and the LLM prompt still shows each candidate's
  position, so this ablation removes graph *scoring*, not graph *knowledge*.
- **`model_version` is the model name** (`phi4-mini`), or `none` in deterministic mode. The
  quantisation is not recorded separately; the fingerprint covers model settings.
- **Evidence rows are written with the run.** This completes the Phase 4 item that was deferred to
  "when `/analyze` cites evidence".

Findings:

1. **The LLM does not beat the deterministic baseline on these fixtures.** Rank-1 accuracy was 5/8
   for `deterministic` against 4/8 for `full`, with the same rollbacks, at about 300 times the
   latency. This matches Phase 6 finding 2.
2. **The structure is what produces correct answers.** `llm_only` was worst: 3/8, and 4 wrong
   rollbacks, including routine deploys on the benign `anom-fx-09` and ambiguous `anom-fx-10` where
   every scored mode said `no_action`.
3. **The sample is small and optimistic**: one run per mode on 8 labelled fixtures written by the
   same author as the corpus. The Week 8–9 evaluation needs real injected faults, which depends on
   issue #6 (no standing traffic).
4. **Latency under load:** while this evaluation ran, a live `deterministic` request took 2.6–11 s
   instead of about 50 ms. Its retrieval embedding call queued behind LLM generations on the same
   GPU. In production, deterministic requests would share Ollama with LLM requests.

---

### M2 detector upgrade (2026-09-16) — integration, not a plan phase

M2 upgraded their detector after Phase 8 was measured. This was not planned work for M3; it is
consumed here because the new fields are exactly what Phase 4 finding 4 and Phase 5 finding 2 said
was missing.

**Consumed:** `detector`, `contributors` (measured value, baseline and score per service and
metric), a real `evidence_window`, `related_deploy_ids`, `in_deploy_window`, and the `liveness`
metric the staleness detector emits when a service publishes nothing at all. Every field is
optional in `app/models.py`, so events recorded before the upgrade still parse.

**Where each is used:** `app/prompts.py` renders them in the anomaly block, including a plain
sentence explaining what a `liveness` anomaly means; `app/scoring.py` puts the measured values in
the anomaly evidence summary and the detector and contributors in its payload; `app/retrieval.py`
maps `liveness` to the `service_crash` and `dependency_failure` fault types for the pre-filter.

**Outcome — the Phase 4 crash limitation is resolved by M2's signal.** `anom-fx-11` is a real
staleness event (a `service_crash` injection on payment) added as a fixture. Scoring ranks payment
first with no change to the weights, because the detector makes the silent service anomalous and it
is the deepest anomalous service. It is now a passing case in `test_fixture_root_cause_ranks_first`,
alongside the three older crash fixtures that remain `xfail` — they are the same fault without the
signal, and they document what the service can and cannot do on its own.

**Not re-run:** the Phase 6 and Phase 8 evaluation numbers were measured on the older 10 fixtures
and are left as recorded. Re-running them fairly needs the testbed under real traffic (issue #6),
which is the Week 8–9 evaluation, not this change.

**Ablation re-run on 2026-09-16** (11 fixtures, 4 modes, then 3 runs per mode across the three
scored modes - 143 runs in total, every one stored): the Phase 8 conclusion is unchanged. `deterministic` 6/9 at 50 ms, `no_graph` 6/9, `full` 5/9, `llm_only` 4/9 with 7 of 11
rank-1 actions proposing a rollback. `anom-fx-11` is correct in all four modes. The `no_graph`
versus `full` gap is one fixture on one run per mode, so it is noise and is recorded as such. Full
table and reading in `README.md`, "Ablation re-run".

**Verified live on 2026-09-16, on a real fault with real traffic.** With M1's `load-generator`
holding 5 rps, a `service_crash` injection on payment was detected by M2's staleness detector 31 s
later as a `liveness` anomaly, and `/analyze` ranked payment first and was answered by the LLM, not
the fallback. The cause text names what the detector measured rather than retelling a past incident.
Full numbers in `README.md`, "Live end-to-end verification". This is the first time the path has run
end to end outside fixtures, and it closes the Phase 4 and Phase 5 crash findings in practice as
well as on paper. It is one run of one fault type, so it proves the path, not the accuracy.

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
