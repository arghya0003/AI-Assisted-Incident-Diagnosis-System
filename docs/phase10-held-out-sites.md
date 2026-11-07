# Held-out evaluation on two dummy sites (M2)

StreamBox (video streaming) and BuildBoard (CI/CD) are two dummy sites that share
nothing with Sock Shop. Neither detector, grouper nor ranker was written or tuned
with them in view, so a full fault suite on each is a held-out test of the system.

This page records one run per site, on 2026-11-07, with the detector and ranker
exactly as deployed. Nothing was tuned before or after. The run label is
`ewma-untuned`; any later run on these suites with changed thresholds must be
labelled as tuned.

## Result in one table

| Metric | Target | StreamBox | BuildBoard |
| --- | --- | --- | --- |
| Faults detected | — | 9 of 9 | 9 of 9 |
| Detection latency (median / p95) | median under 60 s | 13.7 s / 39.6 s | 34.7 s / 40.0 s |
| Top-3, injected service named by the alert | above 70% | 100% | 89% |
| Top-3, any first alert in the window | above 70% | **67%** | **56%** |
| MRR, injected service named by the alert | above 0.6 | 0.87 | 0.81 |
| MRR, any first alert in the window | above 0.6 | **0.48** | **0.43** |
| Evidence validity | 100% | 100% | 100% |
| False-positive rate | under 1 per hour | **4.36 per hour** (3 alerts, 41.3 min) | **7.28 per hour** (5 alerts, 41.2 min) |

Read the two Top-3 / MRR rows together. The first of each pair only counts cases
where the detector's alert already named the injected service, so it is an upper
bound. The second takes the first alert in the fault window whatever it names,
which includes cascades. Quoting only the first would flatter the ranker.

Detection is 9 of 9 on both sites. The false-positive target is missed on both,
and the unfiltered ranking targets are missed on both.

## How the runs were made

- Suites `streaming` and `cicd`: 9 faults each, covering every fault class that
  applies to the site plus its `app_fault` modes (see
  `services/dummy-service/README.md`).
- 300 s between faults, 120 s warm-up, 600 s of quiet observation at the end.
- Each fault lasts 60 to 180 s. A fault window is injection to recovery plus 90 s;
  an alert outside every window is a false positive.
- The runner scored only the site's own alerts. It set aside 12 events (StreamBox
  run) and 42 events (BuildBoard run) that named other services.
- Ranking was scored afterwards with `evaluation-runner attribute` over the same
  window.
- StreamBox ran 10:26 to 11:38 UTC, BuildBoard 12:22 to 13:34 UTC. They ran one
  after the other, never together.
- The four generated reports are kept unedited in
  [`held-out-runs/2026-11-07/`](held-out-runs/2026-11-07/):
  `streaming-live.md`, `streaming-attribution.md`, `cicd-live.md`,
  `cicd-attribution.md`.

## StreamBox

| Fault | Target | Latency |
| --- | --- | --- |
| `service_crash` | `stream-storage` | 39.6 s |
| `service_crash` | `stream-catalog` | 39.5 s |
| `bad_deploy_latency` | `stream-playback` | 39.5 s |
| `dependency_timeout` | `stream-playback` (cause `stream-cache`) | 8.3 s |
| `config_error` | `stream-web` (cause `stream-playback`) | 8.4 s |
| `memory_exhaustion` | `stream-storage` | 35.8 s |
| `resource_exhaustion` | `stream-playback` | 8.3 s |
| `app_fault` slow_storage | `stream-storage` | 13.7 s |
| `app_fault` cache_flush | `stream-cache` | 8.6 s |

`config_error` was missed on 2026-11-01 (Phase 2, 2.5 minutes after the previous
fault) and is detected here, with 5 minutes between faults. This run does not
show why. The gap changed and so did the amount of leftover disturbance.

### Ranking

| Fault | Cause | Ranked | RR |
| --- | --- | --- | --- |
| `dependency_timeout` | `stream-cache` | `stream-playback`, `stream-web`, then **`stream-cache`** | 0.33 |
| `config_error` | `stream-playback` | `stream-web`, then **`stream-playback`** | 0.50 |
| the other seven | | cause ranked first | 1.00 |

Top-1 is 78% (7 of 9). Both misses are cascades where the symptom is upstream of
the cause and the upstream service is ranked first.

### False positives (3)

| Alert | Services | Metric | Time | After a fault ended |
| --- | --- | --- | --- | --- |
| `…-0023` | `stream-playback` | p99 | 10:58:50 | 3.0 min |
| `…-0032` | `stream-playback`, `stream-web` | p99 | 11:15:16 | 4.9 min, 6 s before the next injection |
| `…-0038` | `stream-web` | p99 | 11:28:05 | 4.7 min |

All three are p99-only latency alerts. None coincides with an alert elsewhere
within 40 s, except an `orders` alert 9 s after the second one. The last 10
minutes of the run were clean. Cause not established.

## BuildBoard

| Fault | Target | Latency |
| --- | --- | --- |
| `service_crash` | `ci-deployer` | 40.0 s |
| `service_crash` | `ci-runner` | 39.7 s |
| `bad_deploy_latency` | `ci-scheduler` | 39.6 s |
| `dependency_timeout` | `ci-scheduler` (cause `ci-runner`) | 8.2 s |
| `config_error` | `ci-web` (cause `ci-pipeline`) | 8.3 s |
| `memory_exhaustion` | `ci-artifacts` | 34.7 s |
| `resource_exhaustion` | `ci-pipeline` | 34.8 s |
| `app_fault` stuck_runner | `ci-runner` | 8.1 s |
| `app_fault` flaky_build | `ci-runner` | 8.1 s |

### Ranking

| Fault | Cause | Ranked | RR |
| --- | --- | --- | --- |
| `dependency_timeout` | `ci-runner` | `ci-scheduler`, `ci-artifacts`, `ci-pipeline` (cause absent from top 3) | 0.00 |
| `config_error` | `ci-pipeline` | `ci-web`, `ci-scheduler`, then **`ci-pipeline`** | 0.33 |
| the other seven | | cause ranked first | 1.00 |

Top-1 is 78%, Top-3 89%.

### False positives (5)

| Time | Services | Metric |
| --- | --- | --- |
| 12:30:05 | `ci-web` | p99 |
| 12:42:54 | `ci-artifacts`, `ci-web` | p95, p99 |
| 13:21:46 | six `ci-*` services | error rate, p95, p99 |
| 13:24:51 | `ci-scheduler` | p99 |
| 13:31:56 | `ci-artifacts`, `ci-pipeline`, `ci-runner`, `ci-web` | p95, p99 |

**All five fell within 20 s of an alert on another system**: StreamBox, Sock Shop
or both. At 12:42:50 all three systems alerted together, and again at 13:24:49 to
13:24:52. Three unrelated systems slowing in the same second points to something
shared (this host runs 42 containers in one Docker VM) and not to BuildBoard's own
behaviour. That is a correlation. The cause was not measured.

## Quiet-stack baseline

Before either suite, with the stack freshly started and no fault injected:

- 09:20 to 10:00 UTC: **0 alerts** on either site, 1 on Sock Shop.
- Warm-up (09:00 to 09:20): 3 Sock Shop alerts, 0 on the sites.
- 12:20:00 to 12:21:55, between the suites, with no fault active: alerts on all
  three systems at once, before the BuildBoard run began. Not counted in its
  false-positive rate.

So the sites are silent on a quiet host and noisy while a suite is running or
just after it. Whether that is host load, aftermath of the faults, or something
else was not separated.

## What this does not show

- **One run per site, 9 faults each.** Two are `app_fault`. Per-class rates and
  percentiles from one scenario each are anecdotes.
- **The false-positive rate is not a clean detector property.** It comes from
  roughly 41 minutes of gaps between faults, on a loaded host. 4.36 and 7.28 per
  hour are what was measured, not a floor.
- **No tuning was tried**, so nothing here says the false-positive target can be
  met on these sites.
- **Detection latency clusters** near 35 to 40 s for `service_crash`,
  `bad_deploy_latency` and `memory_exhaustion` on both sites, and near 8 to 14 s
  for most others. Why was not investigated.
- **Alert timestamps jitter by seconds**, and a 1-minute p95 lags a fade, so
  latencies are good to a few seconds.
- **Not checked:** whether the ranker's answers in these runs came from the model
  or from its deterministic fallback. Rows on the dashboard for unattributed
  alerts show the fallback.
- Sock Shop was not re-measured here. Its results are in `phase9-detection.md`.

## Next

1. Find out what the false positives are: container CPU and memory against the
   alert times, and a quiet window with the host otherwise idle.
2. Decide whether to tune. If so, it is a labelled second run, compared against
   the numbers above.
3. The two cascade misses on each site (`dependency_timeout`, `config_error`) are
   the cases for M3 to look at.
