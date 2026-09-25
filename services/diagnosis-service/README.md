# diagnosis-service (Member 3)

Given an `anomaly_id`, returns a ranked, evidence-cited list of root-cause hypotheses with a
proposed remediation from a fixed action vocabulary. Nothing is ever executed — output is for
human approval via M4. Scores candidates deterministically (deploy proximity, dependency-graph
position, co-anomaly, incident similarity), then uses a local LLM only to explain and re-rank,
and drops any hypothesis citing evidence that was not provided to it.

Build spec and phase status: [PLAN.md](PLAN.md).

**Status:** Phase 8 — all planned phases built. M2's detector writes every
`anomalies.detected` event to the shared `anomalies` table (`timescaledb/init/005_anomalies.sql`,
shape agreed in PR #10); this service reads it and never writes real events. For a stored anomaly,
the full pipeline:

1. ranks possible root causes deterministically: the anomalous services plus everything they
   call, scored on recent deploys, graph distance, being the deepest anomalous service, and
   similarity to retrieved past incidents (`GET /candidates/{anomaly_id}` shows this ranking);
2. gives the top candidates to the LLM, which writes and ranks up to 3 hypotheses under a JSON
   schema that ties each hypothesis to one candidate and that candidate's own evidence and actions;
3. validates the reply and retries with the rejection reasons; after 3 invalid attempts, or if no
   model could be reached, it returns the deterministic ranking with templated causes instead;
4. passes every hypothesis through the **evidence guardrail**. A hypothesis citing any id the
   service did not supply (the anomaly, the evidence it produced, and the deploy and incident ids
   put in the prompt) is dropped entirely. If every LLM hypothesis is dropped, the deterministic
   ranking is returned.

`POST /analyze` therefore always returns a contract-valid response in which every evidence id
resolves to a real anomaly, deploy or incident.

**Pipeline modes**, for the evaluation's ablations. Choose one per request with `?mode=`, or set the
default with `PIPELINE_MODE`:

| Mode | What runs |
| --- | --- |
| `full` (default) | Everything above |
| `deterministic` | Scoring and retrieval only; the scorer's ranking is the answer. This is the no-LLM baseline. |
| `no_graph` | The full pipeline with the graph-proximity weight set to 0 and the other weights rescaled |
| `llm_only` | The anomaly, related anomalies and raw deploys go straight to phi4-mini, with no scores, graph or past incidents. There is no fallback: a failure returns an empty answer, so the mode measures the LLM alone. |

**Stored runs and caching.** Every `/analyze` run is stored in the `analyses` and `hypotheses`
tables, and the evidence behind the ranking in `evidence`. A repeat request for the same anomaly,
mode, model and configuration returns the stored answer (`X-Cache: hit`).

- **When a stored answer is not reused:** after a fallback or a failure, or when the run was made
  before the anomaly's 2-minute co-anomaly window closed.
- **Configuration changes:** a change to the prompt, weights or model settings changes the
  configuration fingerprint (shown in `/health`), so old answers are not served.
- **Forcing a new run:** add `?refresh=true`.

## API

| Method | Path | Body | Returns |
| --- | --- | --- | --- |
| `GET` | `/health` | — | `{"status":"ok","service","version","pipeline_mode","config_fingerprint","database"}` |
| `POST` | `/analyze` | `{"anomaly_id": "anom-0001"}` | `{"hypotheses":[{rank, cause, confidence, evidence_ids[], proposed_action}]}` — see CONTRACTS.md |
| `GET` | `/hypotheses/{anomaly_id}` | — | Stored `/analyze` runs for an anomaly, newest first: `[{analysis_id, pipeline_mode, answered_by, model_version, config_fingerprint, llm_attempts, guardrail_rejected, latency_ms, fallback_reason, created_at, hypotheses[{rank, service, cause, confidence, evidence_ids, proposed_action}]}]`. Optional `?mode=` and `?limit=` (default 20). 404 for an unknown anomaly. |
| `GET` | `/stats` | — | Evidence-guardrail counters since the service started, split into LLM and deterministic hypotheses: checked, rejected, and responses fully rejected. The counters reset on restart. |
| `GET` | `/candidates/{anomaly_id}` | — | Debug: `{anomaly_id, anomalous_services, related_anomaly_ids, weights, retrieval_status, similar_incidents[], candidates[{rank, service, score, signals, distance, deploy_id, evidence_ids}], evidence[]}`. Read-only, no LLM generation; 404 and 503 as for `/analyze`. `retrieval_status` is `ok`, `empty_corpus` or `embedding_unavailable`; retrieval problems never fail the request. |

Interactive docs: `http://localhost:8000/docs`.

| `/analyze` status | Meaning |
| --- | --- |
| 200 | anomaly found; hypotheses returned |
| 404 | no anomaly with this id has been received |
| 422 | malformed request body |
| 503 | TimescaleDB unreachable, or a migration below not applied |

A model failure never produces an error status. Two response headers say how the answer was made:

| Header | Values |
| --- | --- |
| `X-Diagnosis-Mode` | `llm` (phi4-mini's hypotheses); `deterministic` (the scorer's ranking, by design in `deterministic` mode); `deterministic_fallback` (the scorer's ranking because the LLM failed, with causes prefixed `Deterministic ranking (LLM not used):`); or `llm_failed` (no answer, `llm_only` mode only) |
| `X-Pipeline-Mode` | The mode that ran: `full`, `no_graph`, `llm_only` or `deterministic` |
| `X-Analysis-Id` | The stored run's id, as listed by `GET /hypotheses/{anomaly_id}` |
| `X-Cache` | `hit` when a stored answer was returned, otherwise `miss` |
| `X-Persisted` | `false` if the run could not be stored; the answer is still returned |
| `X-LLM-Attempts` | `0`–`3`: generation attempts made; `0` when the prompt could not fit the context budget |
| `X-Guardrail-Rejected` | Number of hypotheses the evidence guardrail dropped for this answer |

`/health` returns 200 whenever the process is up. `database` is `ok`, `unreachable` or
`schema_missing`, so an outage is visible without the container being restarted for something a
restart can't fix.

`proposed_action` is one of `rollback_deploy:<deploy_id>`, `restart_service:<service>`,
`scale_service:<service>`, or `no_action`. M4 confirmed the vocabulary and implements all four.

Each verb is offered only where its evidence exists, so the model chooses between real options
rather than inventing one:

| Action | Offered when |
| --- | --- |
| `rollback_deploy:<id>` | The candidate's own deploy landed within about 7 minutes before onset |
| `restart_service:<name>` | M2's staleness detector reports **this** service silent - a `liveness` anomaly naming it (issue #23) |
| `no_action` | Always available |
| `scale_service:<name>` | **Never.** Nothing measured here shows a service is overloaded; offering it would be a guess |

A restart is deliberately offered only to the silent service itself, not to the callers that
merely report errors because of it. Gating matters: when restart was offered to every candidate
during Phase 6, phi4-mini proposed one for every fixture, benign cases included.

## Run

**Database migrations.** The `anomalies` table belongs to M2 (`005_anomalies.sql`, plus
`007_anomalies_upgrade.sql` for a volume created before PR #10). This service adds
`005_diagnosis.sql` (incidents, hypotheses, pgvector) and `006_diagnosis_analyses.sql` (stored
runs). Init scripts run automatically only on a fresh TimescaleDB volume; on an existing one,
apply them once, in order (safe to re-run):

```
docker compose exec -T timescaledb psql -U postgres -d metrics -v ON_ERROR_STOP=1 -f /docker-entrypoint-initdb.d/005_anomalies.sql
docker compose exec -T timescaledb psql -U postgres -d metrics -v ON_ERROR_STOP=1 -f /docker-entrypoint-initdb.d/007_anomalies_upgrade.sql
docker compose exec -T timescaledb psql -U postgres -d metrics -v ON_ERROR_STOP=1 -f /docker-entrypoint-initdb.d/005_diagnosis.sql
docker compose exec -T timescaledb psql -U postgres -d metrics -v ON_ERROR_STOP=1 -f /docker-entrypoint-initdb.d/006_diagnosis_analyses.sql
```

In Git Bash, prefix the command with `MSYS_NO_PATHCONV=1`.

**With the whole stack** (from the repo root):

```
docker compose up -d --build diagnosis-service
curl localhost:8000/health
```

**Standalone, without Docker** (from `services/diagnosis-service/`):

```
python -m venv .venv
.venv\Scripts\activate            # Windows; use: source .venv/bin/activate elsewhere
pip install -r requirements-dev.txt
uvicorn app.main:app --port 8000
```

**Tests.** From the repo root with the stack up:

```
bash services/diagnosis-service/scripts/test_in_docker.sh              # all tests
bash services/diagnosis-service/scripts/test_in_docker.sh --fixtures   # tests, then load fixtures
```

`python -m pytest` from the host also works, but the database tests in `tests/test_db.py` skip
unless `localhost:5432` really is Docker's TimescaleDB. On this dev machine a Windows
PostgreSQL service owns port 5432, so the script runs the tests inside the compose network
instead. Database tests roll back and leave nothing behind.

**Fixtures.** `fixtures/anomalies/anom-fx-01..10.json` are hand-written anomaly events, each with
a `_fixture` block recording the injected fault and true root cause. Load them with
`--fixtures` above so `/analyze` accepts their ids. They are stored with `source='fixture'`,
which evaluation must exclude, and re-loading replaces them.

**Incident corpus.** `corpus/incidents/*.md` holds 61 past-incident write-ups: 35 synthetic Sock
Shop incidents and 26 paraphrased public postmortems (sources and caveats in `corpus/README.md`).

**No ingestion step is needed** (issue #19). Their embeddings are committed alongside them in
`corpus/embeddings.jsonl`, and the service loads them into an empty `incidents` table at startup,
without calling any embedding model. A clean `docker compose up` therefore has a working corpus,
and `GET /health` reports `corpus_incidents` and a `retrieval` status so an empty one is never
silent. Embedding is deterministic for a fixed model and text, so a committed vector is exactly
what ingestion would have produced; each line stores the sha256 of the text it was made from, and
a vector whose text has since changed is refused rather than used.

After editing a write-up, regenerate them (needs Ollama):

```bash
python corpus/ingest.py --write-embeddings   # re-embed, rewrite the file, and load
python corpus/ingest.py --offline            # load the committed vectors, no Ollama
```

`test_in_docker.sh --ingest` still re-embeds and loads in place; `--compare` prints
vector-versus-hybrid retrieval for every fixture. Both need Ollama on the host.

**Retrieval at request time still needs Ollama**, because the anomaly query must be embedded too.
A seeded corpus removes the manual step and the silent-empty-corpus trap; it does not make
retrieval work on a machine with no embedding model, where `retrieval_status` is
`embedding_unavailable` and incident similarity scores 0 for every candidate.

**Generation provider.** `LLM_PROVIDER` is `openrouter` by default, with
`nvidia/nemotron-3-super-120b-a12b:free` as `LLM_MODEL` and `qwen/qwen3.8-27b:free` as
`LLM_FALLBACK_MODELS`. The key comes from the gitignored `.env` at the repo root as
`OPENROUTER_API_KEY`, passed through `docker-compose.yml`; without it the service still starts and
answers, always from the deterministic ranking, and says so at startup.

The fallback chain is for **availability only** — a rate limit, an outage, a timeout. A reply that
arrives and breaks the contract is the model's own behaviour and is retried against the *same*
model, so a stored answer is always attributable to the model that wrote it. `model_version` on
each stored analysis records which model that was, not which was configured.

Set `LLM_PROVIDER=ollama` to generate locally with `phi4-mini` instead. That path is kept so the
phi4-mini results recorded below can be reproduced, and so the system can be demonstrated without
an API key. **Embeddings are always local**: the `incidents` table is `vector(768)` from
`nomic-embed-text` and the committed corpus vectors were produced with it.

**Configuration** — environment variables, defaults in `app/settings.py`:
`PG_HOST`, `PG_PORT`, `PG_DB`, `PG_USER`, `PG_PASSWORD`, `OLLAMA_URL`,
`LLM_PROVIDER`, `OPENROUTER_API_KEY`, `OPENROUTER_URL`, `LLM_FALLBACK_MODELS`,
`LLM_REASONING_EFFORT` `low`, `LLM_MODEL`, `EMBED_MODEL`, `LLM_CONTEXT_TOKENS`.
Scoring: `SCORE_WEIGHT_DEPLOY` 0.40, `SCORE_WEIGHT_GRAPH` 0.25, `SCORE_WEIGHT_CO_ANOMALY` 0.20,
`SCORE_WEIGHT_INCIDENT` 0.15 (must sum to 1, checked at startup), `DEPLOY_LOOKBACK_MINUTES` 30,
`DEPLOY_DECAY_MINUTES` 10, `CO_ANOMALY_WINDOW_SECONDS` 120. Retrieval: `RETRIEVAL_MODE` `hybrid`
(or `vector`), `RETRIEVAL_TOP_K` 3, `OLLAMA_TIMEOUT_SECONDS` 60. LLM: `LLM_TEMPERATURE` 0.1,
`LLM_TIMEOUT_SECONDS` 120, `LLM_MAX_ATTEMPTS` 3, `LLM_MAX_OUTPUT_TOKENS` 3072,
`LLM_RESPONSE_RESERVE_TOKENS` 3072, `PROMPT_MAX_CANDIDATES` 5, `PROMPT_MIN_CANDIDATES` 3.
`PIPELINE_MODE` `full` (or `no_graph`, `llm_only`, `deterministic`). The prompt text is in
`app/prompts/*.txt`. The
container reaches Ollama on the host via `host.docker.internal`, which requires Ollama to listen
on `0.0.0.0` (`OLLAMA_HOST`).

---

## Phase 0 results — 2026-09-13

Reproduce with:

```
bash   services/diagnosis-service/scripts/phase0_stack_check.sh
python services/diagnosis-service/scripts/phase0_llm_bench.py [--num-ctx 8192]
```

### Host

| Item | Value |
| --- | --- |
| GPU | NVIDIA GeForce RTX 3050 Ti Laptop, 4096 MiB (about 3295 MiB free before any model loads — Windows holds the rest) |
| RAM | 15.2 GB |
| Free disk | 49 GB |
| Ollama | 0.34.0, `OLLAMA_HOST=0.0.0.0` |
| Docker | 29.5.2, Compose v5.1.3 |

### Upstream stack — `phase0_stack_check.sh`: 18 pass, 0 fail, 0 warn

| Check | Result |
| --- | --- |
| M1 stack | 24 containers running, none restarting or failed |
| `metrics` | 34,565 rows, newest sample 4 s old — pipeline live |
| `deploys` | 55 rows |
| `evidence` | exists, 0 rows (expected — M3 is its writer) |
| `fault_scenarios` | exists, 0 rows — no faults injected on this volume yet |
| pgvector | **available** in the TimescaleDB image |
| `metrics.raw`, `deploys.events` | exist, 3 partitions each |
| `anomalies.detected` | exists, **1 partition** — auto-created on M2's first publish, not by `kafka-init` |
| M2 output | real anomalies present on the topic |
| Ollama from a container | reachable via `host.docker.internal:11434` |

### LLM — `phase0_llm_bench.py`, 10 JSON-mode runs on a Phase 6-shaped prompt

| Metric | `num_ctx` 8192 | `num_ctx` 4096 |
| --- | --- | --- |
| Valid, schema-conforming JSON | **10/10** | 10/10 |
| Runs citing an evidence ID not in the prompt | **0/10** | 0/10 |
| Latency p50 (min / max) | **5.0 s** (4.4 / 5.4) | 8.5 s (7.8 / 30.2) |
| Generation speed | 24.7 tok/s | 32.9 tok/s |
| Resident size | 4.2 GB | 3.6 GB |
| Placement | **44% CPU / 56% GPU** | 36% CPU / 64% GPU |
| `nomic-embed-text` | 768 dims, about 0.9 s warm (27.5 s cold) | — |

**Reading:**

- `phi4-mini` is a defensible choice on this hardware for correctness: JSON compliance and
  citation discipline were perfect on this prompt. Ten runs is not a guarantee; the Phase 6
  retry loop and Phase 7 guardrail stay.
- It does **not** fit entirely on the GPU at either context size. At 8K, 19 of 33 layers are
  offloaded to the GPU and the rest run on CPU; the KV cache alone is 1 GB. Dropping to 4K
  moves only a few more layers across and did not reduce latency in this run (the 30.2 s max
  includes a model reload). **Keep `num_ctx` at 8192** — it buys prompt room without a
  measured latency cost.
- About 5 s per `/analyze` is acceptable for a human-approval workflow. It is a cost for the
  Week 8-10 evaluation runs, which is why Phase 8 caches responses.
- The first model load crashed Ollama's runner once (`0xc0000409`) and returned HTTP 500; the
  immediate retry loaded normally. Phase 6's client must treat a 500 on the first call as
  retryable, not fatal.
- The benchmark prompt is a stand-in written for Phase 0, not the final Phase 6 prompt. If
  results diverge later, suspect the prompt before the hardware.

---

## Phase 5 retrieval results — 2026-09-13

Reproduce with `bash services/diagnosis-service/scripts/test_in_docker.sh --ingest --compare`.
Corpus: 61 incidents embedded with `nomic-embed-text` (symptoms section only; see PLAN.md, Phase 5).

### `anom-fx-07` (catalogue-db connection pool exhausted), top 3, judged by hand

The fixture is an error-rate and p99-latency anomaly on `catalogue` and `front-end`, with no
deploy involved.

| Rank | Incident | Similarity | Judgement |
| --- | --- | --- | --- |
| 1 | `incident-0008` catalogue deploy lowers its CPU limit and throttles the service | 0.771 | **Partly relevant.** Right service and a similar symptom profile, but the wrong mechanism: it points towards a deploy, which this fixture doesn't have. |
| 2 | `incident-0022` catalogue deploy lowers the connection pool maximum from 50 to 5 | 0.752 | **Relevant.** Same mechanism (requests queue on an exhausted pool), though there the trigger was a deploy. |
| 3 | `incident-0020` reporting job exhausts catalogue-db connections | 0.748 | **Most relevant.** Connection exhaustion with no deploy, errors and latency together, front-end slowing. The closest match in the corpus, yet ranked third. |

Verdict: 2 of 3 relevant and 1 partly relevant. The best match is present but not first, and
the scores are only 0.023 apart.

### Pure vector vs hybrid

Compared on all 10 fixtures. The top 3 is identical in 9. On `anom-fx-03` (payment latency),
pure vector returns `incident-0031` (a front-end alert, outside the candidate set) as #3, and
hybrid replaces it with `incident-0007` (a shipping bad deploy). **Hybrid is kept, as marginally
better.** At this corpus size its filter rarely removes anything, because candidate sets and
metric-to-fault mappings are broad.

### Before the symptoms-only change

The first ingest embedded title plus full body. `anom-fx-07` then retrieved `incident-0001`,
`0004` and `0031`: none about connection pools, two about other services entirely. Two generic
incidents (`0031`, `0004`) filled 14 of the 30 top-3 slots across the fixtures. The comparison
that led to embedding symptoms only is in PLAN.md, Phase 5 outcome.

### Reading

- Retrieval works mechanically and helps when the anomaly's symptoms are distinctive, but it
  can't distinguish causes the anomaly doesn't describe; crash fixtures retrieve crash
  incidents for the wrong service.
- Similarities cluster between about 0.63 and 0.77, so the `incident_similarity` signal is weak.
- These numbers are optimistic: the synthetic corpus and the fixtures share an author and fault
  types, and no public postmortem reached any fixture's top 3.

---

## Phase 6 LLM results — 2026-09-14

Reproduce with `EVAL_RUNS=10 bash services/diagnosis-service/scripts/test_in_docker.sh --eval`.
The run used `phi4-mini`, `num_ctx` 8192, temperature 0.1, and 3 attempts. Each fixture runs
with its scenario context (deploys and related anomalies) plus live retrieval.

| Fixture | True cause | 1st-try valid | Retried | Fallback | p50 ms | p95 ms | Rank 1 = true cause | Rank-1 action |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `anom-fx-01` | catalogue | 10 | 0 | 0 | 15,998 | 19,502 | 10/10 | `rollback_deploy:dep-fx-01-inj` |
| `anom-fx-02` | orders | 10 | 0 | 0 | 16,422 | 17,535 | 10/10 | `rollback_deploy:dep-fx-02-inj` |
| `anom-fx-03` | payment | 10 | 0 | 0 | 12,201 | 13,006 | 10/10 | `rollback_deploy:dep-fx-03-inj` |
| `anom-fx-04` | catalogue | 10 | 0 | 0 | 13,654 | 15,145 | 0/10 | `no_action` |
| `anom-fx-05` | user | 10 | 0 | 0 | 13,094 | 13,548 | 0/10 | `no_action` |
| `anom-fx-06` | carts | 10 | 0 | 0 | 14,240 | 14,881 | 0/10 | `rollback_deploy:dep-fx-06-bg2` (wrong: a routine orders deploy) |
| `anom-fx-07` | catalogue | 10 | 0 | 0 | 15,807 | 17,434 | 10/10 | `no_action` |
| `anom-fx-08` | shipping | 10 | 0 | 0 | 16,204 | 16,972 | 10/10 | `rollback_deploy:dep-fx-08-inj` |
| `anom-fx-09` | none (benign) | 10 | 0 | 0 | 13,284 | 14,482 | n/a | `no_action` |
| `anom-fx-10` | none (ambiguous) | 10 | 0 | 0 | 11,367 | 11,576 | n/a | `no_action` |

**Overall:**
- **Validity:** 100/100 contract-valid, all on the first attempt; 0 retries, 0 fallbacks.
- **Latency:** p50 14.1 s, p95 17.0 s. Live `POST /analyze` over HTTP (10 calls): p50 12.1 s,
  p95 14.0 s.
- **Accuracy:** rank 1 is the true cause in 50/80 runs, exactly matching the deterministic
  scorer's rank 1.
- **Normalisation:** 35/100 replies were adjusted (a repeated candidate dropped, or reordered).
- **Token estimate:** Ollama's prompt-token counts were 0.71–0.76 of the estimate.

### Reading

- **Reliability is solved; accuracy and truthfulness are not.** The schema, validation and
  fallback make `/analyze` always valid.
- **Ranking follows the scorer.** The crash fixtures (`anom-fx-04/05/06`) stay wrong, as Phase 4
  predicted. M2's staleness detector has since made this case solvable; see "M2 detector upgrade".
- **Explanations can invent facts.** On live `/analyze` calls for fixtures, whose deploys are not
  stored, phi4-mini wrote causes such as "catalogue's recent deployment lowered its CPU limit"
  (`anom-fx-01`) and "carts release enabled debug logging" (`anom-fx-10`). Both were copied from
  similar past incidents and stated as current facts. Treat `cause` as unverified until PLAN.md
  Phase 6 finding 1 is resolved.
- **Background deploys lead to a wrong rollback.** One `anom-fx-06` proposal would roll back an
  unrelated orders deploy, which illustrates issue #7.

### After fixes A and B

**A:** past incidents appear in the prompt without their root-cause and resolution text. **B:** a
cause that claims a deploy for a candidate with no recent deploy is rejected and retried.
Measured with the same 100-run harness.

| Measure | Before | After |
| --- | --- | --- |
| Valid on first attempt / retried / fallback | 100 / 0 / 0 | 56 / 32 / 12 |
| Latency p50 / p95 | 14.1 s / 17.0 s | 15.6 s / 40.2 s |
| Rank 1 = true cause | 50/80 | 50/80 |
| Wrong rank-1 rollbacks | 10 | 14 (4 more from the fallback on `anom-fx-05`) |

- **Invented deploy stories are gone** from the live answers for `anom-fx-01/06/07/10`, and a
  rejected cause is never returned.
- **The cost is retries and fallbacks.** At temperature 0.1, retries mostly repeat the same
  rejected sentence.
- **Titles still carry stories:** one rejected `anom-fx-07` cause was incident-0008's title,
  verbatim.
- **Next-step options** are listed in PLAN.md, Phase 6 follow-up.

---

## Phase 7 evidence guardrail results — 2026-09-14

| Check | Result |
| --- | --- |
| Poisoned replies citing `ev-9999` / `dep-fake-001` (tests, all 10 fixtures) | Never reach a response. Partly poisoned: only the clean hypothesis survives. Fully poisoned: the deterministic ranking is returned. |
| Real phi4-mini, 1 run per fixture | 10/10 valid; **0** hypotheses dropped |
| Live `POST /analyze` (3 calls) | `X-Guardrail-Rejected: 0`; `GET /stats` showed 7 checked, 0 rejected |

The guardrail doesn't fire on phi4-mini, because Ollama's response schema already restricts
evidence ids while the model generates. It is the enforced backstop, and it becomes the active
filter if the service moves to a provider without schema enforcement.

---

## Phase 8 ablation results — 2026-09-14

> Superseded by "Ablation re-run" at the end of this file. Kept because it is what Phase 8 was
> signed off against, and because the two runs used different fixtures and a different prompt.

Reproduce, storing every run:

```
EVAL_RUNS=1 EVAL_ARGS="--modes full,no_graph,llm_only,deterministic --persist" \
  bash services/diagnosis-service/scripts/test_in_docker.sh --eval
```

Each of the 10 fixtures ran once in each mode, with its scenario context. Eight fixtures have a
known root cause.

| Mode | Rank-1 service = true cause | Answered by | Needed a retry | Latency p50 / p95 | Rank-1 rollbacks (wrong) |
| --- | --- | --- | --- | --- | --- |
| `deterministic` | **5/8** | scorer 10 | — | **52 ms** / 77 ms | 6 (2 wrong) |
| `full` | 4/8 | LLM 8, fallback 2 | 5/10 | 15.0 s / 40.7 s | 6 (2 wrong) |
| `no_graph` | 5/8 | LLM 9, fallback 1 | 3/10 | 13.1 s / 38.3 s | 6 (2 wrong) |
| `llm_only` | 3/8 | LLM 10 | 2/10 | 11.9 s / 26.1 s | 7 (**4 wrong**) |

The guardrail dropped 0 hypotheses in every mode.

**Per fixture:**
- **All modes:**
  - The bad-deploy fixtures (`anom-fx-01/03/08`) get the injected rollback.
  - The crash fixtures (`anom-fx-04/05/06`) are wrong.
- **`deterministic`, `full` and `no_graph`:**
  - `anom-fx-02` gets the injected rollback.
  - Two wrong rollbacks: routine deploys on `anom-fx-05` and `anom-fx-06`.
- **`llm_only`:**
  - `anom-fx-02`: rolls back front-end's routine deploy instead of orders' injected one.
  - Benign `anom-fx-09` and ambiguous `anom-fx-10`: proposes rolling back routine deploys; every other mode chose `no_action`.
- **`full`:** `anom-fx-07` put front-end first after check B's retry.

**Reading:**
- **The LLM adds nothing measurable here.** On these fixtures it does not beat the
  deterministic baseline, which is also about 300 times faster.
- **Structure does the work.** Removing scoring, graph and retrieval (`llm_only`) gave the fewest
  correct causes and the most harmful proposals.
- **Treat this as indicative.** It is one run per mode on 8 labelled fixtures written by the
  same author as the corpus; the evaluation weeks need real injected faults (issue #6).

**Querying stored runs in one statement:**

```sql
SELECT a.anomaly_id, a.pipeline_mode, a.answered_by, a.llm_attempts, a.latency_ms,
       h.service AS rank1_service, h.proposed_action AS rank1_action, h.confidence
FROM analyses a
LEFT JOIN hypotheses h ON h.analysis_id = a.analysis_id AND h.rank = 1
WHERE a.anomaly_id LIKE 'anom-fx-%'
ORDER BY a.anomaly_id, a.pipeline_mode;
```

## M2 detector upgrade — 2026-09-16

M2's detector now publishes more than it did when Phases 2–8 were measured. Every new field is
optional, so an event recorded earlier still parses and still works.

| New field | What the service does with it |
| --- | --- |
| `detector` (`ewma`, `zscore`, `cusum`, `static`, `staleness`) | Shown in the prompt and stored on the anomaly evidence, so a reader can tell a threshold trip from a statistical one. |
| `contributors` (per service and metric: `value`, `baseline`, `score`) | The prompt's "observed and baseline values" line, which used to read "not provided by the anomaly detector", and the anomaly evidence summary. |
| `evidence_window` (a real window, no longer zero-width) | Printed in the prompt. |
| `related_deploy_ids`, `in_deploy_window` | Printed in the prompt when set, so the model sees which deploys the detector itself implicated. |
| `liveness` metric, from the staleness detector | Treated as a crash signal in retrieval: it pre-filters to `service_crash` and `dependency_failure` incidents, and the prompt explains that the service stopped reporting metrics altogether. |

**This fixes the Phase 4 crash limitation, and M2 fixed it, not M3.** A crashed service used to be
invisible: it stops reporting, so it was never in the anomaly and never scored. The staleness
detector names it directly, which makes it anomalous and the deepest anomalous service, and the
existing weights then rank it first. Fixture `anom-fx-11` — copied from a real event the detector
produced after a `service_crash` injection on payment — ranks the crashed service first with no
change to scoring. The three older crash fixtures (`anom-fx-04/05/06`) are the same fault seen
without that signal, and stay wrong.

The evaluation numbers above predate this and were measured on the older 10 fixtures; they are not
re-run here, because a fair re-run needs the testbed under real traffic (issue #6).

## Live end-to-end verification — 2026-09-16

The first check of the whole path on a **real fault with real traffic**, rather than on fixtures.
M1's `load-generator` (issue #6) makes this possible: the testbed used to idle at ~0.2 req/s, where
an injected fault moved no metric.

**Setup.** Full Compose stack, `load-generator` at 5.008 achieved rps with 0 failures, Ollama on the
host with phi4-mini and nomic-embed-text.

| Step | Result |
| --- | --- |
| Inject | `POST :5001/faults` `{service: payment, fault_type: service_crash, duration_s: 90}` → `scn-service-crash-1789542547`, `t_inject` 07:09:07Z |
| Detect | M2's staleness detector, `anom-20260916T070941-d16f49-0002`, metric `liveness`, severity high, onset 07:09:10.5Z, detected 07:09:41.6Z (**31 s**), contributor value 31.086 against baseline 30.0 |
| Rank | Deterministic: **payment first**, score 0.534 (graph 1.00, co-anomaly 1.00, incident similarity 0.56, deploy 0.00) |
| Answer | `answered_by=llm`, 3 attempts, 21.5 s, 0 guardrail rejections, persisted. Rank 1 **payment** — the ground truth — with `no_action` |

**The cause text improved because of the new prompt fields.** The same anomaly answered by the
pre-upgrade image gave "Payment process killed; checkouts fail (similar to incident-0014)"; the
current image gives "The payment service stopped reporting liveness metrics, indicating a possible
crash or unreachability, similar to past incidents where the payment service itself crashed
(incident-0014)". The first borrows a past incident's story, the second states what the detector
measured. That is the `liveness` explanation line working.

**The 3 attempts were fix A working, not a failure.** Attempts 1 and 2 were rejected because
phi4-mini wrote causes claiming a deploy or release for payment, which has no deploy listed; it
dropped them on attempt 3. Worth watching: three attempts is the cap, so this answer was one retry
away from the deterministic fallback.

**Caveats.** One run, one fault type, one service. It shows the path works end to end on a real
event; it is not an accuracy measurement.

**Gotcha worth knowing:** `scripts/test_in_docker.sh` builds the image but runs its tests in a
throwaway container — it never recreates the running service. A live check straight after it will
silently exercise the *old* code. Use `docker compose up -d --build diagnosis-service` first, and
`?refresh=true` on `/analyze`, since a stored answer for the same anomaly and config is served again.

**Host resources with both running** (16 GiB machine, 4 GiB GPU): Docker 2.91 GiB inside a 7 GB WSL
cap, phi4-mini 2.77 of 4 GiB VRAM, 1.84 GB Windows memory still available, 22.3 of 31.2 GB committed,
page file 5.8% used. Docker and Ollama coexist with headroom once WSL is capped and the page file is
fixed at 16 GB.

## Ablation re-run — 2026-09-16

The Phase 8 ablation repeated after the M2 detector upgrade, on 11 fixtures (`anom-fx-11` is new)
with the prompt that now carries measured values. Nine fixtures have a known root cause.

```
EVAL_RUNS=1 EVAL_ARGS="--modes full,llm_only,no_graph,deterministic --persist" \
  bash services/diagnosis-service/scripts/test_in_docker.sh --eval
```

| Mode | Rank-1 = true cause | Answered by | Needed a retry | Latency p50 / p95 | Rank-1 rollbacks |
| --- | --- | --- | --- | --- | --- |
| `deterministic` | **6/9** | scorer 11 | — | **50 ms** / 62 ms | 6 |
| `no_graph` | **6/9** | LLM 10, fallback 1 | 4/11 | 10.3 s / 41.5 s | 6 |
| `full` | 5/9 | LLM 10, fallback 1 | 6/11 | 13.0 s / 30.1 s | 5 |
| `llm_only` | 4/9 | LLM 11 | 1/11 | 9.7 s / 14.3 s | **7** |

**Reading:**

- **The Phase 8 conclusion holds.** The LLM still does not rank better than the deterministic
  scorer, now at about 260 times the latency. Nothing here argues for making the LLM the ranker.
- **`llm_only` is still the worst and the most dangerous.** 7 of its 11 rank-1 actions were
  `rollback_deploy`, including routine background deploys on the benign `anom-fx-09` and the
  ambiguous `anom-fx-10`, where every scored mode said `no_action`.
- **`no_graph` beating `full` (6/9 versus 5/9) is noise, not a result.** One run per mode, and the
  gap is a single fixture (`anom-fx-05`). It is not evidence against the graph signal, and it should
  not be quoted as one without a multi-run re-run.
- **`anom-fx-11` was correct in all four modes**, `llm_only` included. The crash case that failed in
  every mode before now succeeds in every mode, because M2's staleness detector names the silent
  service. See "M2 detector upgrade".
- **`full` fell back to the scorer on `anom-fx-11`** after three rejected attempts, and still ranked
  payment first. That is the fallback doing its job.
- **Fix A does most of the retry work.** Almost every rejection was "the cause mentions a deploy or
  release, but *X* has no recent deploy listed" — 6 of 11 runs in `full`. The check earns its place,
  and it shows phi4-mini reaches for a deploy explanation by default.
- **The prompt token estimate is no longer conservative.** Actual/estimated ratios ran 0.76 to 1.32,
  against the Phase 6 assumption that 3.0 characters per token overestimates by ~15%. Harmless at
  current prompt sizes; recorded in `app/prompts.py` so nobody relies on the old margin.

**Caveat, stated plainly:** one run per mode on 9 labelled fixtures. Differences of one fixture are
within noise. A multi-run sweep (`--runs 3`) is the follow-up; this table is enough to say the
conclusion did not change, and not enough to rank the LLM modes against each other.

## Three runs per mode — 2026-09-16

The single-run ablation above left one question open: `no_graph` scored 6/9 against `full`'s
5/9, on one run per mode. This repeats the three scored modes with three runs each — 99 runs,
every one stored.

```
EVAL_RUNS=3 EVAL_ARGS="--modes full,no_graph,deterministic --persist" \
  bash services/diagnosis-service/scripts/test_in_docker.sh --eval
```

| Mode | Rank-1 = true cause | Answered by LLM | Fell back to scorer | Needed a retry | p50 / p95 latency |
| --- | --- | --- | --- | --- | --- |
| `deterministic` | **18/27** | — | — | 0/33 | **51 ms** / 60 ms |
| `full` | **18/27** | 31/33 | 2/33 (6%) | 17/33 | 14.2 s / 31.3 s |
| `no_graph` | 17/27 | 24/33 | 9/33 (**27%**) | 20/33 | 16.6 s / 44.0 s |

**Reading:**

- **The single-run gap was noise.** `no_graph` beating `full` did not survive three runs, and
  the earlier table's caveat was right to refuse to quote it.
- **`full` matches `deterministic` fixture by fixture**, not merely in total: the two are
  correct and incorrect on exactly the same cases. The LLM follows the scorer's ranking and
  supplies the explanation. It neither improves the ranking nor damages it.
- **The graph's real contribution is reliability.** Removing it raised the fallback rate from
  6% to 27% of runs, and the rejections say why: without graph positions phi4-mini claims
  deploys that do not exist (`catalogue-db` alone accounted for 12 rejections). Some of
  `no_graph`'s 17/27 is therefore the scorer answering, not the model.
- **Safety held.** On the benign `anom-fx-09` and ambiguous `anom-fx-10`, every mode chose
  `no_action` on every run. The wrong rollbacks (`anom-fx-05`, `anom-fx-06`) also occur in
  `deterministic`, so they come from the scorer's deploy weighting (issue #7), not the LLM.
- **Retries are dominated by fix A**: 17/33 runs in `full` needed one, almost always because
  the cause claimed a deploy for a service that had none.
- **Token estimate:** ratios reached 1.40, worse than the 1.32 recorded above. `app/prompts.py`
  carries the corrected note.

**Caveat, stated plainly:** at temperature 0.1 most fixtures returned the same answer on all
three runs, so this is closer to nine fixtures checked for consistency than to 27 independent
samples. It settles run-to-run noise. It does not establish that nine fixtures are enough, and
it is still fixtures rather than real injected faults (issue #22).

## Moving generation to OpenRouter — 2026-09-25

phi4-mini ran only on a machine with the model pulled and a GPU to spare, which no other machine on
the team had. Every integration run from M4's side answered `deterministic_fallback` (issue #20).
Since the deployed system diagnoses a running website, it is online by definition, so an API is the
honest dependency. Generation moved to OpenRouter; embeddings stayed local.

**Verified before any code was written.** The concern was that the safety design would not port:
the response schema uses a per-candidate `anyOf` that binds each hypothesis to one service's
evidence ids and actions, plus `minItems`/`maxItems`/`maxLength` bounds that exist because
constrained decoding once looped until the output limit. One probe with the real `anom-fx-01`
prompt settled it — `nvidia/nemotron-3-super-120b-a12b:free` accepted the schema with
`strict: true` and validated first try:

```
HTTP 200 in 9.9s | cost 0 | VALIDATES: True | errors: []
rank 1 catalogue | rollback_deploy:dep-fx-01-inj | cites anom-fx-01, dep-fx-01-inj, anom-fx-01-p99
"High latency on catalogue coincides with a recent deploy (dep-fx-01-inj) occurring 0.2 minutes
 before onset and a config diff that introduced an inefficient loop."
```

**Then end to end through the API** on the stored `anom-fx-11`: `X-Diagnosis-Mode: llm`,
**1 attempt**, 0 guardrail rejections, 14.5 s, `model_version` stored as the answering model. The
cause named what the detector measured: "observed value 31.06 vs baseline 30, indicating the
service stopped reporting metrics".

**Two findings worth carrying into the evaluation:**

- **Reasoning tokens dominate the output budget.** nemotron spent **569 of 719** completion tokens
  reasoning, for a single hypothesis. The old 768-token cap would have truncated three hypotheses
  mid-JSON, failing validation and burning retries against a 50-request daily limit. Hence
  `LLM_MAX_OUTPUT_TOKENS` 3072 and `LLM_REASONING_EFFORT` `low`.
- **Free endpoints rate-limit often.** `qwen/qwen3.8-27b:free` returned HTTP 429 ("temporarily
  rate-limited upstream") on the first probe. A 429 is therefore **not** retried on the same model:
  it clears on someone else's schedule, and switching model is instant and free where waiting is
  neither.

**Free-tier limits that shape how the evaluation can run:** 20 requests/minute and **50/day** until
at least $10 of credits is purchased, after which 1,000/day. A model comparison over the 11 fixtures
is 11 requests and fits comfortably; the 99-run three-mode sweep does not, without credits.

**Not done here:** embeddings still need Ollama at request time, because the anomaly query must be
embedded too. On a machine with no embedding model, `retrieval_status` is `embedding_unavailable`
and incident similarity scores 0 — visible in `GET /health`, not silent. Removing that dependency
means an embedding model whose dimensions match `vector(768)`, or a schema migration and
re-running the Phase 5 retrieval comparison.
