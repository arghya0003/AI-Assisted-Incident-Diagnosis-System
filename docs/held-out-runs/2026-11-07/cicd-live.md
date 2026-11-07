# Live evaluation run

Generated 2026-11-07T13:34:19+00:00 — regenerate with `docker compose run --rm evaluation-runner ...` (see docs/phase9-detection.md).

## Headline metrics

| Metric | Measured | Target | Verdict |
| --- | --- | --- | --- |
| Detection latency | 34.7 s | Median under 60 s | met |
| Root-cause accuracy (Top-3) | not measured | Top-3 above 70% | — |
| Ranking quality (MRR) | not measured | MRR above 0.6 | — |
| False-positive rate | 7.28 /hour | Under 1 per hour | MISSED |
| Evidence validity | not measured | 100% | — |

Root-cause accuracy, ranking quality and evidence validity score M3's ranker, and a `live` run does not ask it anything - the fault is already over by the time a diagnosis would be useful to score. Run `evaluation-runner attribute` against the same window for those three; they are shown here as not measured rather than as zero.

## Detection by fault type

| Fault type | Scenarios | Detected | Of observable | Misattributed | Missed | Unobservable | Median latency | p95 latency |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `app_fault` | 2 | 2 (100%) | 100% | 0 | 0 | 0 | 8.1 s | 8.1 s |
| `bad_deploy_latency` | 1 | 1 (100%) | 100% | 0 | 0 | 0 | 39.6 s | 39.6 s |
| `config_error` | 1 | 1 (100%) | 100% | 0 | 0 | 0 | 8.3 s | 8.3 s |
| `dependency_timeout` | 1 | 1 (100%) | 100% | 0 | 0 | 0 | 8.2 s | 8.2 s |
| `memory_exhaustion` | 1 | 1 (100%) | 100% | 0 | 0 | 0 | 34.7 s | 34.7 s |
| `resource_exhaustion` | 1 | 1 (100%) | 100% | 0 | 0 | 0 | 34.8 s | 34.8 s |
| `service_crash` | 2 | 2 (100%) | 100% | 0 | 0 | 0 | 39.8 s | 40.0 s |
| **overall** | **9** | **9 (100%)** | **100%** | **0** | **0** | **0** | **34.7 s** | **40.0 s** |

**Detected** is over every scenario attempted — the number that cannot flatter. **Of observable** excludes scenarios where the target service emitted no request-level telemetry, so nothing could have been detected. Both are shown; quoting only the second would let a broken testbed look like a good detector.

## False positives

5 alert(s) fired outside any fault window across 41.2 minutes of quiet observation (7.28 per hour).

## Per-scenario detail

| Scenario | Fault type | Ground truth | Outcome | Latency | Matched event |
| --- | --- | --- | --- | --- | --- |
| `scn-service-crash-1794054281` | `service_crash` | `ci-deployer` | detected | 40.0 s | anom-20261107T122521-30231e-0054 |
| `scn-service-crash-1794054641` | `service_crash` | `ci-runner` | detected | 39.7 s | anom-20261107T123121-30231e-0060 |
| `scn-bad-deploy-latency-1794055001` | `bad_deploy_latency` | `ci-scheduler` | detected | 39.6 s | anom-20261107T123721-30231e-0064 |
| `scn-dependency-timeout-1794055391` | `dependency_timeout` | `ci-scheduler` | detected | 8.2 s | anom-20261107T124319-30231e-0073 |
| `scn-config-error-1794055781` | `config_error` | `ci-web` | detected | 8.3 s | anom-20261107T124950-30231e-0080 |
| `scn-memory-exhaustion-1794056171` | `memory_exhaustion` | `ci-artifacts` | detected | 34.7 s | anom-20261107T125646-30231e-0087 |
| `scn-resource-exhaustion-1794056561` | `resource_exhaustion` | `ci-pipeline` | detected | 34.8 s | anom-20261107T130316-30231e-0094 |
| `scn-app-fault-1794057041` | `app_fault` | `ci-runner` | detected | 8.1 s | anom-20261107T131049-30231e-0098 |
| `scn-app-fault-1794057461` | `app_fault` | `ci-runner` | detected | 8.1 s | anom-20261107T131749-30231e-0104 |

## Notes

- Detector under test: `ewma-untuned` (as deployed).
- 28 anomaly event(s) observed in total.
- Suite `cicd` ran on the `ci-*` site only. 42 event(s) about other services were set aside before scoring, so the false-positive rate counts this site's alerts alone; Sock Shop's alerts say nothing about how the detector does here. Gap after each fault: 300 s.
- Testbed reset before warm-up: 52 accumulated order(s) cleared from orders-db, so `orders` started at its baseline latency.

