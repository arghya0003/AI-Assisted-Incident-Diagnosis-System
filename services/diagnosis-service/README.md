# diagnosis-service (Member 3)

Given an `anomaly_id`, returns a ranked, evidence-cited list of root-cause hypotheses with a
proposed remediation from a fixed action vocabulary. Nothing is ever executed — output is for
human approval via M4. Scores candidates deterministically (deploy proximity, dependency-graph
position, co-anomaly, incident similarity), then uses a local LLM only to explain and re-rank,
and drops any hypothesis citing evidence that was not provided to it.

Build spec and phase status: [PLAN.md](PLAN.md).

**Status:** Phase 6 — LLM reasoning. A background consumer stores every `anomalies.detected`
event in the `anomalies` table. For a stored anomaly, the pipeline:

1. ranks possible root causes deterministically: the anomalous services plus everything they
   call, scored on recent deploys, graph distance, being the deepest anomalous service, and
   similarity to retrieved past incidents (`GET /candidates/{anomaly_id}` shows this ranking);
2. gives the top candidates to `phi4-mini`, which writes and ranks up to 3 hypotheses under a JSON
   schema that ties each hypothesis to one candidate and that candidate's own evidence and actions;
3. validates the reply and retries with the rejection reasons; after 3 invalid attempts, or if
   Ollama is unreachable, it returns the deterministic ranking with templated causes instead;
4. passes every hypothesis through the **evidence guardrail**. A hypothesis citing any id the
   service did not supply (the anomaly, the evidence it produced, and the deploy and incident ids
   put in the prompt) is dropped entirely. If every LLM hypothesis is dropped, the deterministic
   ranking is returned.

`POST /analyze` therefore always returns a contract-valid response in which every evidence id
resolves to a real anomaly, deploy or incident.

## API

| Method | Path | Body | Returns |
| --- | --- | --- | --- |
| `GET` | `/health` | — | `{"status":"ok","service","version","pipeline_mode","database","consumer"}` |
| `POST` | `/analyze` | `{"anomaly_id": "anom-0001"}` | `{"hypotheses":[{rank, cause, confidence, evidence_ids[], proposed_action}]}` — see CONTRACTS.md |
| `GET` | `/stats` | — | Evidence-guardrail counters since the service started, split into LLM and deterministic hypotheses: checked, rejected, and responses fully rejected. The counters reset on restart. |
| `GET` | `/candidates/{anomaly_id}` | — | Debug: `{anomaly_id, anomalous_services, related_anomaly_ids, weights, retrieval_status, similar_incidents[], candidates[{rank, service, score, signals, distance, deploy_id, evidence_ids}], evidence[]}`. Read-only, no LLM generation; 404 and 503 as for `/analyze`. `retrieval_status` is `ok`, `empty_corpus` or `embedding_unavailable`; retrieval problems never fail the request. |

Interactive docs: `http://localhost:8000/docs`.

| `/analyze` status | Meaning |
| --- | --- |
| 200 | anomaly found; hypotheses returned |
| 404 | no anomaly with this id has been received |
| 422 | malformed request body |
| 503 | TimescaleDB unreachable, or `005_diagnosis.sql` not applied |

A model failure never produces an error status. Two response headers say how the answer was made:

| Header | Values |
| --- | --- |
| `X-Diagnosis-Mode` | `llm` (phi4-mini's hypotheses), or `deterministic_fallback` (the scorer's ranking with causes prefixed `Deterministic ranking (LLM not used):`) |
| `X-LLM-Attempts` | `0`–`3`: generation attempts made; `0` when the prompt could not fit the context budget |
| `X-Guardrail-Rejected` | Number of hypotheses the evidence guardrail dropped for this answer |

`/health` returns 200 whenever the process is up. `database` is `ok`, `unreachable` or
`schema_missing`, and `consumer` is `running`, `connecting` or `disabled`, so an outage is
visible without the container being restarted for something a restart can't fix.

`proposed_action` is one of `rollback_deploy:<deploy_id>`, `restart_service:<service>`,
`scale_service:<service>`, or `no_action`. The vocabulary is still to be confirmed with M4.
Currently only `no_action` and `rollback_deploy` are ever proposed. A rollback is offered only
for a candidate's own deploy within about 7 minutes before onset. Restart and scale are never
offered, because no signal yet shows a service has failed or is overloaded (PLAN.md, Phase 6
outcome).

## Run

**Database migration.** `timescaledb/init/005_diagnosis.sql` runs automatically only on a
fresh TimescaleDB volume. On an existing one, apply it once (safe to re-run):

```
docker compose exec -T timescaledb psql -U postgres -d metrics -v ON_ERROR_STOP=1 -f /docker-entrypoint-initdb.d/005_diagnosis.sql
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
set CONSUMER_ENABLED=false        # no Kafka outside Docker
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
Shop incidents and 26 paraphrased public postmortems (sources and caveats in
`corpus/README.md`). Embed and load them, re-runnable and updating in place, with
`test_in_docker.sh --ingest`; `--compare` prints vector-versus-hybrid retrieval for every fixture.
Both need Ollama on the host.

**Configuration** — environment variables, defaults in `app/settings.py`:
`KAFKA_BOOTSTRAP`, `PG_HOST`, `PG_PORT`, `PG_DB`, `PG_USER`, `PG_PASSWORD`, `OLLAMA_URL`,
`LLM_MODEL`, `EMBED_MODEL`, `LLM_CONTEXT_TOKENS`, `CONSUMER_ENABLED` (default `true`).
Scoring: `SCORE_WEIGHT_DEPLOY` 0.40, `SCORE_WEIGHT_GRAPH` 0.25, `SCORE_WEIGHT_CO_ANOMALY` 0.20,
`SCORE_WEIGHT_INCIDENT` 0.15 (must sum to 1, checked at startup), `DEPLOY_LOOKBACK_MINUTES` 30,
`DEPLOY_DECAY_MINUTES` 10, `CO_ANOMALY_WINDOW_SECONDS` 120. Retrieval: `RETRIEVAL_MODE` `hybrid`
(or `vector`), `RETRIEVAL_TOP_K` 3, `OLLAMA_TIMEOUT_SECONDS` 60. LLM: `LLM_TEMPERATURE` 0.1,
`LLM_TIMEOUT_SECONDS` 120, `LLM_MAX_ATTEMPTS` 3, `LLM_MAX_OUTPUT_TOKENS` 768,
`LLM_RESPONSE_RESERVE_TOKENS` 1024, `PROMPT_MAX_CANDIDATES` 5, `PROMPT_MIN_CANDIDATES` 3. The
prompt text is in `app/prompts/*.txt`. The
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
  predicted.
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
