# Interface Contracts

Status: **DRAFT — needs sign-off from all four members before Week 3.**
Owner of this file: Member 1 (produces `metrics.raw`, `logs.raw`, `deploys.events`).
Once agreed, changes after Week 5 require the whole team to agree (per project plan, Section 4).

These are the only data shapes members should need to discuss more than once. If a new
shape comes up in conversation twice, it belongs in this file.

## Kafka topics

### `metrics.raw`
Direction: M1 → M2
Producer: M1's per-service Micrometer/OpenTelemetry exporters, one record per metric sample.

```json
{
  "service": "catalogue",
  "metric": "latency_p99_ms",
  "value": 142.3,
  "timestamp": "2026-08-12T20:45:00.123Z",
  "labels": {
    "instance": "catalogue-1",
    "method": "GET /catalogue"
  }
}
```

### `logs.raw`
Direction: M1 → M2
Producer: M1's log shippers per service.

```json
{
  "service": "catalogue",
  "level": "ERROR",
  "message": "...",
  "timestamp": "2026-08-12T20:45:00.123Z",
  "trace_id": "..."
}
```

### `deploys.events`
Direction: M1 → M3
Producer: M1's deploy event emitter (Phase 5).

```json
{
  "deploy_id": "dep-2026-08-12-0007",
  "service": "catalogue",
  "version": "1.4.2",
  "commit_sha": "a1b2c3d",
  "config_diff": "...",
  "timestamp": "2026-08-12T20:40:00.000Z"
}
```

### `anomalies.detected`
Direction: M2 → M4
Producer: M2's EWMA detector, after dedup/grouping.

```json
{
  "anomaly_id": "anom-20260812T204503-0001",
  "services": ["catalogue", "front-end"],
  "metrics": ["latency_p99_ms", "error_rate"],
  "severity": "high",
  "t_detected": "2026-08-12T20:45:03.000Z",
  "t_onset": "2026-08-12T20:44:50.000Z",
  "evidence_window": {
    "start": "2026-08-12T20:40:00.000Z",
    "end": "2026-08-12T20:45:03.000Z"
  },
  "detector": "ewma",
  "in_deploy_window": true,
  "related_deploy_ids": ["dep-2026-08-12-0007"],
  "contributors": [
    {
      "service": "catalogue",
      "metric": "latency_p99_ms",
      "value": 7470.2,
      "baseline": 36.1,
      "score": 4.21,
      "severity": "high",
      "observed_at": "2026-08-12T20:45:03.000Z"
    }
  ]
}
```

`severity` is one of `low`, `medium`, `high` — **resolved by M2**, closing the
open question below. It is derived from how far past its firing threshold the
worst contributing metric went, so it is comparable across detectors that
otherwise produce incomparable scores (a z-score, a CUSUM statistic and a
threshold overshoot).

The first seven fields are the frozen contract. The rest are additive and safe
to ignore:

| Field | Why it is there |
| --- | --- |
| `detector` | Which algorithm produced this, so ablation runs are self-describing. |
| `in_deploy_window` | True if any member service was mid-deploy. A strong prior for M3. |
| `related_deploy_ids` | The deploys implicated, so M3 need not re-derive them by timestamp. |
| `contributors` | Per-metric detail (value, baseline, score) behind the grouped event, so M3 can build `metrics` evidence items without re-querying TimescaleDB. |

**One event per incident.** A single fault trips many metrics across many
services; M2 groups them and emits one event carrying the member list, rather
than one alert per breach. `t_detected` is stamped from the *first* contributing
signal, not from the moment the grouped event is published, so the grouping
delay does not inflate detection-latency measurements.

**`liveness` is a synthetic metric name.** A crashed service disappears from
Prometheus and therefore emits no telemetry at all, so M2 also reports services
that have stopped reporting. Those events carry `"liveness"` in `metrics[]` and
`"detector": "staleness"`. There is no `liveness` row in the `metrics` table —
M3 should read the gap in that service's samples as the evidence, and
`t_onset` marks when the data stopped.

**Durable copy: the `anomalies` table.** Every event published here is also
written to TimescaleDB's `anomalies` table (`timescaledb/init/005_anomalies.sql`)
under the same `anomaly_id`, so M3 can resolve `POST /analyze {anomaly_id}` long
after the event has left the 24h topic. The frozen fields have columns of their
own; `contributors`, `in_deploy_window` and `related_deploy_ids` are kept whole
in a `detail` JSONB column. Kafka remains the live contract: the table is written
after the publish, and a failed write never holds an alert back.

## Evidence model

Evidence is the common structure M3 uses to explain a diagnosis. Each evidence item
belongs to one of the five categories below and keeps a reference to its source record
instead of copying the source data.

```
Evidence
├── Anomaly
├── Metrics
├── Deployment history
├── Service dependencies
└── Similar past incidents
```

Evidence item shape:

```json
{
  "evidence_id": "ev-0001",
  "incident_id": "anom-0001",
  "category": "metrics",
  "source_id": "catalogue:latency_p99_ms:2026-08-12T20:45:00.123Z",
  "service": "catalogue",
  "observed_at": "2026-08-12T20:45:00.123Z",
  "relevance": 0.92,
  "summary": "Catalogue p99 latency increased 4.2x after the anomaly onset",
  "payload": {
    "metric": "latency_p99_ms",
    "value": 598.4,
    "baseline": 142.3
  }
}
```

`category` must be one of `anomaly`, `metrics`, `deployment`, `dependency`, or
`similar_incident`. `source_id` is the ID or stable composite key in the source system;
`payload` contains category-specific details. `relevance` is a number from 0 to 1 and
is used to rank evidence in the diagnosis response.

The five categories map to current and planned sources as follows:

| Category | Source |
| --- | --- |
| `anomaly` | M2 `anomalies.detected`, durable copy in the `anomalies` table |
| `metrics` | TimescaleDB `metrics` / `metrics_1m` |
| `deployment` | TimescaleDB `deploys` / `deploys.events` |
| `dependency` | Service dependency graph above |
| `similar_incident` | M3 incident history and retrieval store |

The database representation is defined in `timescaledb/init/004_evidence.sql`.

## Synchronous REST

### `POST /analyze`
Direction: M4 → M3

Request:
```json
{ "anomaly_id": "anom-0001" }
```

Response:
```json
{
  "hypotheses": [
    {
      "rank": 1,
      "cause": "Bad deploy dep-2026-08-12-0007 to catalogue introduced a latency regression",
      "confidence": 0.81,
      "evidence_ids": ["anom-0001", "dep-2026-08-12-0007", "incident-0042"],
      "proposed_action": "rollback_deploy:dep-2026-08-12-0007"
    }
  ]
}
```

Every `evidence_ids` entry must resolve to a real anomaly, deploy, or incident record. A
hypothesis citing an unknown ID is rejected outright (M3's hallucination guardrail).

## Test fixtures

### Eval hooks
Direction: M2 → all

```json
{
  "scenario_id": "scn-bad-deploy-latency-1757764800",
  "fault_type": "bad_deploy_latency",
  "ground_truth_service": "catalogue",
  "t_inject": "2026-08-12T20:44:00.000Z",
  "t_recovered": "2026-08-12T20:45:30.000Z",
  "status": "recovered"
}
```

The `fault_scenarios` table (`timescaledb/init/003_fault_scenarios.sql`) is the
authority on these values, not any runner's own clock: the injector writes
`t_inject` at the moment the fault actually starts, and scoring latency against
anything else would silently bias every number in the report.

`fault_type` is currently one of `bad_deploy_latency`, `service_crash`,
`db_pool_saturation` — the three the injector can physically produce.

## Service dependency graph (input to M3)

Derived from each service's actual image contents (extracted config files / jar
resources — `traefik.toml`, `config.js`/`endpoints.js`, `application.properties`), not
guessed. See `docs/phase0-decisions.md` for how each edge was confirmed. Feeds M3's
blast-radius traversal.

```
edge-router (Traefik, single entrypoint)
  -> front-end (Node BFF)
       -> catalogue -> catalogue-db (MySQL)
       -> carts -> carts-db (Mongo)
       -> orders -> orders-db (Mongo)
            -> payment, shipping, user  (standard checkout flow)
       -> user -> user-db (Mongo)

shipping -> rabbitmq <- queue-master   (async fan-out, separate from the REST chain above)
```

## Open questions for the team (resolve before Week 3)

- [ ] Confirm topic partitioning/retention on `metrics.raw` — **implemented** as proposed
      (3 partitions, keyed by `service`, `retention.ms=86400000`), pending team sign-off.
      Downsampling-after-24h not yet built.
- [x] Confirm `anomalies.detected` `severity` enum values with M2 — **resolved:**
      `low` | `medium` | `high`, derived from the worst contributing metric's
      overshoot of its firing threshold. See the topic section above.
- [ ] Confirm `proposed_action` vocabulary (fixed enum, not free text) with M3/M4.
