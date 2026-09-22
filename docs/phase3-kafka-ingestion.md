# Phase 3 Notes: Kafka Ingestion Pipeline

## Kafka: KRaft mode, single node
`apache/kafka:3.9.0`, `KAFKA_PROCESS_ROLES: broker,controller` — no ZooKeeper, as the
project plan directs. Two listeners: `PLAINTEXT` (`kafka:9092`) for other containers on
`diagnosis-net`, `EXTERNAL` (`localhost:29092`) for host-side debugging/tools. Confirmed
clean startup via broker logs (`Kafka Server started`, listening on both ports).

## Topics
Created by the one-shot `kafka-init` service (`kafka-topics.sh --create`, runs once then
exits): `metrics.raw`, `logs.raw`, `deploys.events`, `anomalies.detected` (the last added
later — see the issue #5 section below) — 3 partitions each, `retention.ms=86400000`
(24h), matching the proposal already sitting as an open question in `CONTRACTS.md`.
Verified via `kafka-topics.sh --describe`. Partition key is `service` (set as the Kafka
message key by the producer below) — matches CONTRACTS.md's proposal so that all samples
for a given service land on the same partition, preserving per-service ordering.

### `anomalies.detected` was missing from that list (issue #5)
M2's output topic wasn't in `kafka-init`, so it only came into existence when the anomaly
detector first published — which means Kafka auto-created it with broker defaults:
**1 partition and 7-day retention**, instead of the 3 partitions and 24h this file claims
for every topic. The same class of bug Phase 6 fixed for the other three, just on a topic
M1 doesn't produce to, so nothing here ever touched it.

It mattered in three ways: the settings came from broker defaults rather than from the
compose file (exactly the non-reproducibility the Phase 6 fix removed); anomalies outlived
by six days the 24h of metrics they point at, so a replayed anomaly could reference
samples that no longer exist; and the detector's `key=service` bought no ordering or
parallelism on a single-partition topic.

Fixed by adding it to the `kafka-init` loop. The loop also now **converges topics that
already exist**, because `--create --if-not-exists` can't repair one:

```sh
kafka-topics.sh  ... --create --if-not-exists --topic $topic --partitions 3 --config retention.ms=86400000
kafka-topics.sh  ... --alter --topic $topic --partitions 3 || true
kafka-configs.sh ... --alter --entity-type topics --entity-name $topic --add-config retention.ms=86400000
```

`|| true` because altering a topic that already has 3 partitions is an error, not a no-op.
Partitions can only ever be *increased* this way — Kafka won't shrink a topic — which is
fine for the one direction this needs to go. The effect is that an existing dev stack with
the bad 1-partition topic is repaired by an ordinary `docker compose up`, rather than
needing its Kafka volume wiped. `kafka-init` now ends with `--describe` instead of
`--list`, so the settings it actually applied are visible in the bring-up log.

Verify:

```bash
export MSYS_NO_PATHCONV=1   # Git Bash on Windows only
docker compose exec -T kafka /opt/kafka/bin/kafka-topics.sh \
  --bootstrap-server kafka:9092 --describe --topic anomalies.detected
```

## Producer: metrics-bridge
`services/metrics-bridge/` — a small Python service, not a Sock Shop component. Bridges
Phase 2's Prometheus data onto `metrics.raw`:

1. Polls Prometheus's `/api/v1/targets` every 5s to discover which service instances are
   currently healthy (no hardcoded service list — stays in sync with `prometheus.yml`
   automatically).
2. For each instance, runs 7 PromQL queries derived from the `request_duration_seconds`
   histogram and `process_*` metrics found in Phase 2: `request_rate`, `error_rate`,
   `latency_p50_ms`, `latency_p95_ms`, `latency_p99_ms`, `cpu_rate`, `memory_bytes`.
3. Publishes each non-null result as one `metrics.raw` record in the exact
   `{service, metric, value, timestamp, labels{}}` shape from `CONTRACTS.md`, keyed by
   `service`.

Chose to compute percentiles/rates in the bridge (via Prometheus's `histogram_quantile`)
rather than forwarding raw histogram buckets — matches CONTRACTS.md's own example payload
(`"metric": "latency_p99_ms"`, a single number) and avoids an 11-bucket-per-route-per-service
explosion of low-value samples on the topic.

`kafka-python` (the obvious first choice) doesn't work on Python 3.12 — its vendored `six`
shim throws `ModuleNotFoundError: No module named 'kafka.vendor.six.moves'`. Switched to
`kafka-python-ng`, a maintained fork with the same `kafka` import path, drop-in fix.

## Proof it works
Consumed directly from `metrics.raw` with `kafka-console-consumer.sh --from-beginning`:

```
{"service": "carts", "metric": "cpu_rate", "value": 0.0118..., "timestamp": "2026-08-18T17:35:48.881Z", "labels": {"instance": "carts:80"}}
{"service": "carts", "metric": "memory_bytes", "value": 345972736.0, ...}
{"service": "orders", "metric": "cpu_rate", "value": 0.0089..., ...}
```

Real values, not placeholders — `metrics-bridge` logs confirm ~29 samples published per
5s cycle across all 7 instrumented services.

## kafka-ui
`provectuslabs/kafka-ui`, port 8081 → browser UI for browsing topics, reading messages,
watching consumer groups — nicer than shelling into the broker for
`kafka-console-consumer.sh` every time.

It is in the `debug` Compose profile, so a plain `docker compose up -d` does not start it
(it is a JVM app and costs ~300 MiB that the evaluation host would rather give to Ollama).
Start it when you want it:

```bash
docker compose --profile debug up -d kafka-ui
```

## Known gap: logs.raw has no producer yet
The topic exists (schema frozen in `CONTRACTS.md`) but nothing publishes to it yet — no
log-shipping strategy has been picked (options: tail each container's Docker log driver,
or run something like Filebeat/Fluent Bit as a sidecar). Deferred rather than rushed,
since M2's anomaly detection (the actual consumer of `metrics.raw`) doesn't block on logs
existing yet. Revisit once M2 needs log correlation, or in the Phase 6 orchestration pass.

## Known gap: deploys.events has no producer yet
By design — the deploy event emitter is explicitly Phase 5 scope. Topic exists now so the
schema is frozen and M3 can develop against it, per the plan's "freeze contracts early"
guidance.
