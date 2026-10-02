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

## Seven scenario types

The first three were built in Phase 8; the last four, and the current `db_pool_saturation`
mechanism, came with issue #35. See
[the issue #35 section](#issue-35-four-new-fault-classes-and-a-db_pool_saturation-that-lands)
for how each was verified.

- **`bad_deploy_latency`** — records a real deploy via `deploy-emitter` (Phase 5), then
  CPU-throttles the target container via cgroups (`docker update`-equivalent, using the
  Docker SDK's `container.update(cpu_period, cpu_quota)`) for the duration. A deploy that
  quietly regresses per-request CPU cost is a realistic real-world cause of latency
  regressions - this isn't a shortcut, it's a faithful mechanism, and it closes the loop
  with the deploy log M3 will eventually correlate against.
- **`service_crash`** — stops the container, waits, restarts it. Hard outage / dependency-
  cascade trigger.
- **`db_pool_saturation`** — holds `LOCK TABLES sock WRITE` on `catalogue-db` so every
  catalogue query blocks and its connection pool stays checked out. **Only supports
  `service=catalogue`** (MySQL) - `carts`/`orders`/`user` are Mongo-backed and would need a
  separate implementation, not built yet. Documented gap, not silently missing.
- **`dependency_timeout`** — `docker pause`s the service's dependency (default: its edge in
  the dependency graph), so callers' connections are accepted and never answered.
- **`config_error`** — adds `127.0.0.1 <dependency>` to the service's `/etc/hosts`, so the
  dependency refuses connections while the service stays up and answers errors.
- **`memory_exhaustion`** — lowers the container's memory limit (swap capped too) below its
  working set, so the kernel OOM-kills it and `restart: always` brings it back into the same
  limit until the fault ends.
- **`resource_exhaustion`** — tightens the CPU quota in geometric steps, from
  `start_cpu_limit` (0.05) to `cpu_limit` (0.002). It is the one gradual fault, where every
  other one is a step change (issue #34).

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

**`db_pool_saturation`**, original mechanism (30 held connections, 15s): the connections
were real, but catalogue never noticed. See the issue #35 section below for why, and for
the replacement.

## Prerequisite: traffic has to be running (issue #6)
The measurements above were taken under a manual `curl` loop. Without traffic the same
injection changes nothing at all - M3 reproduced this: a 60s throttle of `catalogue` left
p95 flat at 4.8ms and produced no anomaly, because an idle service that is CPU-throttled
is still idle. The stack now runs a `load-generator` service continuously
(`docs/load-generator.md`), so this is the default state rather than something to remember.

Two consequences for the harness:

- **`cpu_limit` now defaults to `0.002`, measured.** Issue #6 asked for a re-check once
  traffic existed. The answer is more interesting than a tweak: **a CPU quota only bites
  when it is below what the service actually uses**, and these are small Go/Node services.
  Under 5 req/s of standing load, catalogue idles at **0.17% of one core**, so the old
  `0.05` left it roughly 30x more CPU than it needed:

  | `cpu_limit` | catalogue p95 during a 90s throttle | Verdict |
  | --- | --- | --- |
  | `0.05` (5%) | 4.8 ms → **4.8 ms** (request rate flat at 2.4/s) | no-op |
  | `0.002` (0.2%) | 4.8 ms → **160 ms** within 30s, peak 270 ms | 33x, detected |

  At `0.002` M2's detector fired on it end to end: `catalogue/latency_p95_ms value=94.5
  baseline=4.83 score=309`, grouped with `front-end` and tagged to the companion deploy.
  Raise the value for a heavier service — `front-end` idles near 1.8% of a core.

  This is the same class of bug as the `db_pool_saturation` cap of 100 against a
  151-connection pool: a fault that runs, records itself, and physically does nothing.
  `evaluation-runner`'s suite passes `cpu_limit` explicitly, so it does not inherit this
  default; its values are now catalogue `0.002` (measured), front-end and orders `0.005`
  (estimated, not yet measured).
- **Every scenario records the load that was running when it was injected.** `POST /faults`
  reads the generator's `/stats` and stores `params.offered_rps_at_inject` on the row. From
  `fault_scenarios` alone, a fault injected into an idle testbed and a detector that simply
  missed one look identical; this makes the difference visible after the fact. The response
  also carries a `warning` if the testbed is under 1 req/s or the generator can't be
  reached - a warning, not a refusal, so a deliberate idle-baseline run is still possible.

## API

- `POST /faults` `{fault_type, service, duration_s?, ...}` → starts a scenario in the
  background, returns immediately with `scenario_id` and `status: running` (plus `warning`
  if the testbed looks idle). Per-type parameters:

  | `fault_type` | Parameters (default) |
  | --- | --- |
  | `bad_deploy_latency` | `cpu_limit` (0.002) |
  | `service_crash` | — |
  | `db_pool_saturation` | — (`service` must be `catalogue`; a `connections` value is ignored) |
  | `dependency_timeout`, `config_error` | `dependency` (the service's edge in the graph; `front-end` → `carts`) |
  | `memory_exhaustion` | `limit_mb` (64, floor 48) |
  | `resource_exhaustion` | `cpu_limit` (0.002), `start_cpu_limit` (0.05), `steps` (6) |

- `GET /faults?limit=` → scenario history with outcomes.
- `GET /fault-types` → the seven supported types.

Safety caps: `duration_s` clamped to 300s; `dependency` limited to Sock Shop containers
(never timescaledb, kafka or the injector itself); `limit_mb` floored at 48 so a JVM can
still start once the fault ends.

## Issue #35: four new fault classes, and a `db_pool_saturation` that lands

Each fault below was injected on the live stack under 5 req/s of standing load and probed
through `edge-router` every ~4s. Times are from the probe, not estimates.

| Fault | Target | During the fault | After |
| --- | --- | --- | --- |
| `db_pool_saturation` (table lock, 40s) | catalogue | `/catalogue` hung past the 12s client timeout for the whole window; queries waiting on `sock` grew to 26 | 10 ms within a second of `UNLOCK` |
| `dependency_timeout` (30s) | catalogue, with catalogue-db paused | `/catalogue` hung past 12s | 10 ms immediately after unpause |
| `config_error` (30s) | front-end → carts | `/cart` answered **500 in ~10 ms** throughout; front-end did **not** restart | 200 immediately |
| `memory_exhaustion` (64 MB, 40s) | carts | OOM-killed repeatedly (`OOMKilled=true`), `/cart` 500s | serving ~18s after the limit was lifted (JVM start) |
| `resource_exhaustion` (90s, 6 steps) | user | `user` `latency_p95_ms` in 15s buckets: 4.9 → 56 → 161 → 215 → 267 → 296 → **328 ms**, a ramp rather than a step | CPU quota back to `-1` |

**Why the old `db_pool_saturation` did nothing.** Catalogue keeps two pooled connections to
`catalogue-db` and reuses them, so filling the server's 151 `max_connections` never
touched it: catalogue never asks for connection 152 (p95 stayed at 4.8 ms through a full
90s fault). Locking the table catalogue reads saturates catalogue's *own* pool, which is
what the class is named for. It must be `LOCK TABLES ... WRITE`. `SELECT ... FOR UPDATE`
takes row locks, and InnoDB serves plain reads from an MVCC snapshot without waiting on
them. Expect a cliff, not a ramp: catalogue stalls outright while the lock is held.

**The lock's safety net, tested.** The risk is an orphaned lock, not a deadlock. This
server has `lock_wait_timeout` = 1 year and `wait_timeout` = 8 hours, and nothing breaks a
table-level stall. So the locking session sets `wait_timeout = duration + 30s` before
locking, unlocks in a `finally`, and refuses to start while another root session is idle
in `socksdb` or anything is waiting on a table lock. Both paths were tested:
- With the injector frozen (`docker compose pause`) during a 20s lock, MySQL dropped the
  session and catalogue recovered about 48s after the lock was taken (duration plus
  grace), with no UNLOCK ever sent.
- With an idle root session open, the injector refused and marked the scenario failed,
  naming the session.

**`config_error` must not target front-end → catalogue.** Front-end (Node 4.8) crashes on a
refused connection to `catalogue` and crash-loops for the whole fault, which makes it an
outage rather than a config error. Against `carts` it stays up and answers 500s, so that
is the default. Node 4 resolves the hostname on every request, so the change takes effect
immediately; no keep-alive connection outlives it. The injected line carries a marker and
is re-applied every 2s, because Docker regenerates `/etc/hosts` when a container restarts.
Without that, a crash would shed the fault early while the scenario still claimed it was
running.

**`memory_exhaustion` restores "unlimited" as host memory.** A `docker update` cannot
remove a memory limit: `0` means "unchanged" and `-1` is rejected ("Minimum memory limit
allowed is 6MB"). An originally unlimited container comes back with a limit equal to the
host's total memory and unlimited swap. That is the same ceiling in practice, but
`docker inspect` shows a number instead of `0` afterwards. If the container is in restart
backoff when the fault ends, the injector restarts it, so recovery doesn't wait out
Docker's doubling delay.

**Interrupted faults are undone at startup.** Every fault cleans up in a `finally`, but
that only runs if the injector lives to the end of the fault. On startup, any scenario
still `running` is undone (unpause, un-throttle, restore memory or hosts, restart a stopped
container) and marked `failed` with the reason. Tested by killing the injector 5s into a
120s `dependency_timeout`: on restart it unpaused `catalogue-db` and catalogue served
normally. One snag: while a dependency is paused, `docker compose up fault-injector`
refuses to start, because compose walks the dependency chain. Use
`docker start incident-diagnosis-system-fault-injector-1`, or unpause by hand.

## Runbook: one fault, end to end

```bash
export MSYS_NO_PATHCONV=1   # Git Bash on Windows only

# 1. Confirm traffic is actually flowing - not that the container is up.
curl -s localhost:5002/stats      # achieved_rps should be near target_rps

# 2. Record the background deploy rate this run ran under (issue #7).
curl -s localhost:5000/healthz    # simulate_interval_seconds

# 3. Inject.
curl -XPOST localhost:5001/faults -H 'content-type: application/json' \
  -d '{"fault_type":"bad_deploy_latency","service":"catalogue","duration_s":60}'

# 4. Check the effect landed in the metrics.
docker compose exec -T timescaledb psql -U postgres -d metrics -c "
  select time_bucket('1 minute', time) as minute,
         max(value) filter (where metric = 'latency_p95_ms')  as p95_ms,
         avg(value) filter (where metric = 'request_rate')    as req_per_s
  from metrics
  where service = 'catalogue' and time > now() - interval '15 minutes'
  group by 1 order by 1;"
```

Steps 1 and 2 are the point: an evaluation run that skips them can't say afterwards
whether a missed detection was a detector problem or an idle testbed.

## What this unblocks
M2's evaluation runner (Phase 8/9 per the original plan) can already query
`fault_scenarios` for ground truth once it exists — `t_inject`, `t_recovered`, and
`ground_truth_service` are exactly what "detection latency" and "which service was it
actually" scoring needs. Nothing about this harness assumes M2 exists yet; it's usable as
soon as it does.
