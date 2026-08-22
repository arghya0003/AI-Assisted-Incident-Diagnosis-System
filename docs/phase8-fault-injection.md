# Phase 8 Notes: Fault Injection Harness (pulled forward)

Phase 7 (integration support) can't happen yet — M2/M3 don't exist as code. This is the
part of Phase 8 that doesn't depend on teammates: a real fault-injection harness against
the testbed, ready for whenever M2's anomaly detector needs faults to detect and an
evaluation runner needs ground truth to score against.

## Design choice: real faults, not simulated metrics
Every scenario physically acts on a running container via the Docker Engine API (the
harness mounts `/var/run/docker.sock` - standard Docker-outside-of-Docker pattern) rather
than injecting fake data points. The point of this harness is to produce a genuine anomaly
that the *actual* monitoring pipeline (Prometheus → Kafka → TimescaleDB, Phases 2-4) has
to detect on its own - faking the metrics would make the whole exercise circular.

## Three scenario types

- **`bad_deploy_latency`** — records a real deploy via `deploy-emitter` (Phase 5), then
  CPU-throttles the target container via cgroups (`docker update`-equivalent, using the
  Docker SDK's `container.update(cpu_period, cpu_quota)`) for the duration. A deploy that
  quietly regresses per-request CPU cost is a realistic real-world cause of latency
  regressions - this isn't a shortcut, it's a faithful mechanism, and it closes the loop
  with the deploy log M3 will eventually correlate against.
- **`service_crash`** — stops the container, waits, restarts it. Hard outage / dependency-
  cascade trigger.
- **`db_pool_saturation`** — opens N real, held MySQL connections directly against
  `catalogue-db` to consume its connection budget. **Only supports `service=catalogue`**
  (MySQL) - `carts`/`orders`/`user` are Mongo-backed and would need a separate
  pymongo-based implementation, not built yet. Documented gap, not silently missing.

Every scenario is recorded in `fault_scenarios` (new table,
`timescaledb/init/003_fault_scenarios.sql`) in CONTRACTS.md's eval-hooks shape
(`scenario_id`, `fault_type`, `ground_truth_service`, `t_inject`), plus `t_recovered` and
`status` for scoring detection latency later.

## Proof it works — three real faults, real recovery, real numbers

**`bad_deploy_latency`** against `catalogue` (2% CPU, 25s): p95 latency measured via
Prometheus jumped from a ~36ms baseline (Phase 2) to **7.47 seconds** during the fault -
a real ~200x regression, not a fabricated one. A companion deploy landed in the log at the
exact same timestamp (`dep-2026-08-22-0004`, `"perf regression: inefficient loop
introduced"`). CPU quota confirmed restored (`docker inspect` → `CpuQuota=-1`) after
recovery.

**`service_crash`** against `payment` (15s): `docker inspect` confirmed
`Status=exited` during the fault, and **Prometheus's own scrape target health
independently reported `payment:80 -> down`** - the fault was severe enough that the
monitoring stack itself noticed, unprompted. Container confirmed `running` again and
scenario marked `recovered` after ~16s.

**`db_pool_saturation`** against `catalogue-db` (30 connections, 15s): `SHOW STATUS LIKE
'Threads_connected'` showed 32 active connections during the fault (30 held + catalogue's
own), dropping back to 2 after release. Real MySQL connections, not a mock. Noted honestly:
with `max_connections=151` on this image, 30 held connections doesn't fully exhaust the
pool - added a `MAX_CONNECTIONS=100` safety cap so nobody accidentally starves the whole
container, but genuinely maxing it out would need `connections` closer to 150.

## API

- `POST /faults` `{fault_type, service, duration_s?, cpu_limit?, connections?}` → starts a
  scenario in the background, returns immediately with `scenario_id` and `status: running`.
- `GET /faults?limit=` → scenario history with outcomes.
- `GET /fault-types` → the three supported types.

Safety caps: `duration_s` clamped to 300s, `connections` clamped to 100 - a forgotten or
malformed request can't run forever or starve a container outright.

## What this unblocks
M2's evaluation runner (Phase 8/9 per the original plan) can already query
`fault_scenarios` for ground truth once it exists — `t_inject`, `t_recovered`, and
`ground_truth_service` are exactly what "detection latency" and "which service was it
actually" scoring needs. Nothing about this harness assumes M2 exists yet; it's usable as
soon as it does.
