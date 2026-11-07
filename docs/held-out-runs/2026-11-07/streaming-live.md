# Live evaluation run

Generated 2026-11-07T11:38:22+00:00 — regenerate with `docker compose run --rm evaluation-runner ...` (see docs/phase9-detection.md).

## Headline metrics

| Metric | Measured | Target | Verdict |
| --- | --- | --- | --- |
| Detection latency | 13.7 s | Median under 60 s | met |
| Root-cause accuracy (Top-3) | not measured | Top-3 above 70% | — |
| Ranking quality (MRR) | not measured | MRR above 0.6 | — |
| False-positive rate | 4.36 /hour | Under 1 per hour | MISSED |
| Evidence validity | not measured | 100% | — |

Root-cause accuracy, ranking quality and evidence validity score M3's ranker, and a `live` run does not ask it anything - the fault is already over by the time a diagnosis would be useful to score. Run `evaluation-runner attribute` against the same window for those three; they are shown here as not measured rather than as zero.

## Detection by fault type

| Fault type | Scenarios | Detected | Of observable | Misattributed | Missed | Unobservable | Median latency | p95 latency |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `app_fault` | 2 | 2 (100%) | 100% | 0 | 0 | 0 | 11.1 s | 13.7 s |
| `bad_deploy_latency` | 1 | 1 (100%) | 100% | 0 | 0 | 0 | 39.5 s | 39.5 s |
| `config_error` | 1 | 1 (100%) | 100% | 0 | 0 | 0 | 8.4 s | 8.4 s |
| `dependency_timeout` | 1 | 1 (100%) | 100% | 0 | 0 | 0 | 8.3 s | 8.3 s |
| `memory_exhaustion` | 1 | 1 (100%) | 100% | 0 | 0 | 0 | 35.8 s | 35.8 s |
| `resource_exhaustion` | 1 | 1 (100%) | 100% | 0 | 0 | 0 | 8.3 s | 8.3 s |
| `service_crash` | 2 | 2 (100%) | 100% | 0 | 0 | 0 | 39.5 s | 39.6 s |
| **overall** | **9** | **9 (100%)** | **100%** | **0** | **0** | **0** | **13.7 s** | **39.6 s** |

**Detected** is over every scenario attempted — the number that cannot flatter. **Of observable** excludes scenarios where the target service emitted no request-level telemetry, so nothing could have been detected. Both are shown; quoting only the second would let a broken testbed look like a good detector.

## False positives

3 alert(s) fired outside any fault window across 41.3 minutes of quiet observation (4.36 per hour).

## Per-scenario detail

| Scenario | Fault type | Ground truth | Outcome | Latency | Matched event |
| --- | --- | --- | --- | --- | --- |
| `scn-service-crash-1794047362` | `service_crash` | `stream-storage` | detected | 39.6 s | anom-20261107T103001-30231e-0010 |
| `scn-service-crash-1794047722` | `service_crash` | `stream-catalog` | detected | 39.5 s | anom-20261107T103601-30231e-0012 |
| `scn-bad-deploy-latency-1794048082` | `bad_deploy_latency` | `stream-playback` | detected | 39.5 s | anom-20261107T104201-30231e-0014 |
| `scn-dependency-timeout-1794048472` | `dependency_timeout` | `stream-playback` | detected | 8.3 s | anom-20261107T104800-30231e-0016 |
| `scn-config-error-1794048862` | `config_error` | `stream-web` | detected | 8.4 s | anom-20261107T105430-30231e-0019 |
| `scn-memory-exhaustion-1794049252` | `memory_exhaustion` | `stream-storage` | detected | 35.8 s | anom-20261107T110127-30231e-0028 |
| `scn-resource-exhaustion-1794049642` | `resource_exhaustion` | `stream-playback` | detected | 8.3 s | anom-20261107T110730-30231e-0030 |
| `scn-app-fault-1794050122` | `app_fault` | `stream-storage` | detected | 13.7 s | anom-20261107T111535-30231e-0034 |
| `scn-app-fault-1794050512` | `app_fault` | `stream-cache` | detected | 8.6 s | anom-20261107T112200-30231e-0035 |

## Notes

- Detector under test: `ewma-untuned` (as deployed).
- 20 anomaly event(s) observed in total.
- Suite `streaming` ran on the `stream-*` site only. 12 event(s) about other services were set aside before scoring, so the false-positive rate counts this site's alerts alone; Sock Shop's alerts say nothing about how the detector does here. Gap after each fault: 300 s.
- Testbed reset before warm-up: 52 accumulated order(s) cleared from orders-db, so `orders` started at its baseline latency.

