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
  "anomaly_id": "anom-0001",
  "services": ["catalogue", "front-end"],
  "metrics": ["latency_p99_ms", "error_rate"],
  "severity": "high",
  "t_detected": "2026-08-12T20:45:03.000Z",
  "t_onset": "2026-08-12T20:44:50.000Z",
  "evidence_window": {
    "start": "2026-08-12T20:40:00.000Z",
    "end": "2026-08-12T20:45:03.000Z"
  }
}
```

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
  "scenario_id": "scn-latency-regression-01",
  "fault_type": "bad_deploy_latency",
  "ground_truth_service": "catalogue",
  "t_inject": "2026-08-12T20:44:00.000Z"
}
```

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
- [ ] Confirm `anomalies.detected` `severity` enum values with M2.
- [ ] Confirm `proposed_action` vocabulary (fixed enum, not free text) with M3/M4.
