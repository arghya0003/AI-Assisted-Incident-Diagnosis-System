# Member 4 Notes: Orchestration, HITL UI & Safety

Owns the system being a system, and owns the safety story (PLAN.md). Two new services:
`services/orchestrator/` (Python/FastAPI, port 8090) and `services/orchestrator-ui/`
(React + TypeScript + Tailwind, built to static files and served by nginx, port 3000).

## Why Python/FastAPI, not Spring Boot

PLAN.md's tech stack for this slice is Spring Boot + React. M1–M3 all ended up as Python
services instead (Spring Kafka was the original plan for M1 too), so orchestrator follows
that precedent rather than introducing the only JVM service in the stack: it borrows
diagnosis-service's own FastAPI/psycopg2/kafka-python shape directly (same `app/settings.py`,
`app/db.py` conventions), which keeps one language and one set of idioms across the whole
pipeline. React + TypeScript + Tailwind for the UI is unchanged from the plan.

## The incident lifecycle

`app/state_machine.py`'s `Orchestrator` is the single place every transition goes through:

```
DETECTED -> ANALYZING -> AWAITING_APPROVAL -> APPROVED
                       \                    -> REJECTED
                        -> ANALYSIS_FAILED  -> AWAITING_APPROVAL (via /reanalyze)
         (any AWAITING_APPROVAL incident) -> EXPIRED (sweeper, on timeout)
```

`ANALYSIS_FAILED` is the state PLAN.md's "handles ... the case where the LLM is slow or
returns garbage" resolves to: `app/diagnosis_client.py` retries M3's `POST /analyze` on
connection errors, timeouts and `503`s, revalidates the response body against this service's
own `Hypothesis` model rather than trusting M3's `response_model` blindly, and gives up after
`DIAGNOSIS_MAX_ATTEMPTS` (default 3) with backoff. A failed incident is never stuck: `POST
/incidents/{id}/reanalyze` retries on demand, and the UI surfaces the failure with a retry
button instead of leaving the incident silently in `ANALYZING`.

Every write goes through `app/db.py`, which commits the incident-table change and its
matching `audit_log` row in one transaction — the audit trail can never show a decision the
incidents table disagrees with, or vice versa.

**Idempotency.** `handle_anomaly` looks the incident up by `anomaly_id` before creating one, so
a Kafka consumer restart that redelivers an uncommitted `anomalies.detected` message reuses
the existing incident instead of opening a duplicate.

## Safety architecture (the headline contribution)

PLAN.md asks for this to be real, not a paragraph in the report — each piece is backed by a
test in `services/orchestrator/tests/`:

**(a) Constrained action space.** `app/models.py`'s `Hypothesis.proposed_action` validator
accepts exactly the same grammar diagnosis-service already emits — `rollback_deploy:<id>`,
`restart_service:<service>`, `scale_service:<service>`, or the bare `no_action` — never free
text. `ACTION_BLAST_RADIUS` maps every verb to its declared blast radius (always
single-service or none, by construction of the grammar), shown in the UI before an operator
approves anything and exposed at `GET /actions`. This resolves CONTRACTS.md's open question
("Confirm `proposed_action` vocabulary ... with M4") by adopting M3's vocabulary rather than
inventing a second one.

**(b) A hard execution gate.** `app/executor.py`'s `execute()` is a log line and nothing else —
no Docker/Kubernetes/HTTP client, no subprocess, no socket. `test_executor.py` parses the
module's own AST and fails the build if any of those imports are ever added, so this is
enforced by a test, not a comment. The orchestrator container in docker-compose.yml also
mounts no Docker socket and holds no infra credentials, unlike `fault-injector` (which mounts
`/var/run/docker.sock` specifically so it *can* act) — the gate holds even if the process were
compromised, because there is nothing reachable to call.

**(c) An immutable audit log.** `audit_log` (`timescaledb/init/008_incidents.sql`) has a
trigger that raises on `UPDATE`/`DELETE`, and every row's `hash` chains to the previous row's
hash (`app/audit.py`), so a row changed by any means other than that trigger's own append path
breaks every hash after it — `GET /audit/verify` walks the chain and reports the first break.
This is tamper-*evidence*, not tamper-*prevention* (a table-owner superuser can still bypass
the trigger and recompute the chain forward); that limitation is stated in `app/audit.py`'s
docstring rather than left implicit, per PLAN.md's "their limitations stated explicitly".

**(d) Rejection feedback as labelled data.** `POST /incidents/{id}/reject` requires a coarse
`reason_category` (wrong root cause / wrong action / insufficient evidence / duplicate /
other) alongside the free-text reason, stored in `rejection_feedback` — aggregable without NLP,
for a future retraining pass on scoring weights or prompts.

## What "human-in-the-loop" actually blocks

`POST /incidents/{id}/approve` requires an `AWAITING_APPROVAL` incident and a valid
`hypothesis_rank`; anything else (unknown incident, wrong state, unknown rank) is rejected
with 404/409/422 before the executor is ever reached. There is no code path from a detected
anomaly to `execute()` that does not pass through this endpoint — the REST API is the only
caller of `Orchestrator.approve`.

## Live feed

`GET /ws` (via `app/ws.py`'s `ConnectionManager`) pushes one JSON message per lifecycle
event (`{"event": "incident_awaiting_approval", "incident": {...}}`) to every connected
approval-UI client. The Kafka-consumer and sweeper threads call `Orchestrator`'s `broadcast`
callback from their own threads; `ConnectionManager.publish` hands the event to each
connection's `asyncio.Queue` via `call_soon_threadsafe`, so no cross-thread mutation of
asyncio state happens directly.

## Frontend

`services/orchestrator-ui/` — Vite + React 19 + TypeScript + Tailwind v4, ~25 modules, no
router (two tabs: Incidents, Audit log). Talks to the backend through `/api` and `/ws`, which
`nginx.conf` proxies to the `orchestrator` container in production and `vite.config.ts`'s dev
server proxies to `localhost:8090` for local development — same-origin either way, so the
frontend never needs CORS handling. The incident detail view shows the anomaly evidence
(services, metrics, contributing signals), each ranked hypothesis with its confidence, blast
radius and cited evidence ids, and the Approve/Reject/Request-more-info panel; a terminal
incident shows who decided it, when, and whether the stubbed executor logged intent.

## Inspectable evidence

PLAN.md requires more than *showing* evidence ids: "an engineer should be able to click a
hypothesis and see the deploy diff and the similar past incident that justified it". Every
cited id in the UI is therefore a button, and `GET /evidence/{evidence_id}` resolves it to
the record behind it — the deploy with its `config_diff`, the anomaly event, or M3's past
postmortem — which the detail panel renders inline.

M3 cites evidence in several shapes and all of them have to resolve:

| Shape | Example | Resolves to |
| --- | --- | --- |
| bare anomaly id | `anom-20260922T121220-eda98b-0001` | the `anomalies` row |
| bare deploy id (deterministic ranking) | `dep-2026-09-22-0035` | the `deploys` row, **including `config_diff`** |
| bare corpus id | `incident-0001` | M3's `incidents` postmortem |
| `ev:<anomaly>:deployment:<id>` | structured | as above, by category |
| `ev:<anomaly>:metrics:<service>:<metric>:<ts>` | structured | described without a database read |
| `ev:<anomaly>:dependency:a->b` | structured | the graph edge, no database read |

Two details that are easy to get wrong, both covered by tests:

- **The `metrics` source id contains colons of its own** (`catalogue:latency_p99_ms:` plus an
  ISO timestamp, which has two more). Parsing bounds the split at 3, so everything after the
  third colon stays with the source id instead of the timestamp being truncated.
- **A cross-member table that is missing must not break approvals.** The resolver checks
  `to_regclass` before reading `deploys`/`anomalies`/`incidents` and degrades to
  `kind: "unknown"`. An approver losing one evidence card is much better than the approval
  screen failing because a teammate's migration has not been applied.

An id that resolves to nothing returns `kind: "unknown"` rather than 404 — M3's guardrail
already guarantees every cited id existed when the analysis ran, so a miss means the record
has aged out of retention, and the panel says so rather than showing an error.

## Verification

### Offline

- **Backend:** 53 tests (`services/orchestrator/tests`, `pytest`) against an in-memory fake
  store and a fake diagnosis client covering the full state machine (idempotent anomaly
  intake, analysis success/failure/retry, approve/reject/request-info/expiry, the action
  vocabulary, the hash-chain, evidence resolution, and the executor's lack of outbound capability), plus the FastAPI
  routes via `TestClient` with dependency overrides — no live Postgres/Kafka needed, same
  pattern diagnosis-service's own test suite uses.
- **Frontend:** `npm run build` (TypeScript project build + Vite production bundle) and
  `npm run lint` (oxlint) both clean.

### Against the live stack

`docker compose up -d --build`, 23 containers, then PLAN.md's Definition of Done scenario:

| Step | Result |
| --- | --- |
| `bad_deploy_latency` injected against `catalogue` | M2 detected it ~10 s later (target: under 60 s) |
| Orchestrator consumed `anomalies.detected` | Incident opened, `DETECTED -> ANALYZING` |
| M3 `POST /analyze` | Rank-1 named the real deploy `dep-2026-09-22-0001` at 0.843 confidence, action `rollback_deploy:dep-2026-09-22-0001` — the injected fault's ground truth |
| `AWAITING_APPROVAL` -> approve | `execution_logged=true`; `catalogue`'s container start time unchanged, so the stubbed executor never acted |
| Second incident (`service_crash` on `user`) -> reject | Row written to `rejection_feedback` with its category |
| WebSocket feed | `incident_awaiting_approval` pushed to a connected client in real time |
| Direct SQL `UPDATE`/`DELETE` on `audit_log` | Both refused by the trigger; `GET /audit/verify` reported the chain intact |
| `orchestrator-ui` via nginx | Served, and its `/api/*` proxy to the orchestrator worked |

Ollama was not available on that machine, so `/analyze` answered
`deterministic_fallback` — which means the LLM-unavailable path is now covered end to end,
while the `answered_by=llm` path remains covered only by M3's own tests.

**What the live run caught that the tests could not.** M4's table was originally named
`incidents`, colliding with M3's RAG-corpus table of the same name: `CREATE TABLE IF NOT
EXISTS` silently no-opped, the next index failed on a column M3's table lacks, `audit_log`
and `rejection_feedback` were never created, and the orchestrator came up reporting
`database: schema_missing`. Every unit test passed throughout, because they run against an
in-memory fake store and never touch the real schema. Hence `orchestrator_incidents`.
