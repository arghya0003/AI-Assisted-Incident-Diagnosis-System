# Attribution evaluation

Generated 2026-11-07T13:39:52+00:00 · scenarios recorded since 2026-11-07T12:19:52+00:00

**9 of 9 scenario(s) scored.** A scenario is scored only if the detector raised an anomaly for it and the ranker returned hypotheses; one that never reached the ranker measures detection, not attribution, and counting it here would charge the ranker for a detection miss.

| Metric | Measured | Target | Verdict |
| --- | --- | --- | --- |
| Root-cause accuracy (Top-1)* | 78% | — | — |
| Root-cause accuracy (Top-3)* | 89% | Top-3 above 70% | met |
| Ranking quality (MRR)* | 0.81 | MRR above 0.6 | met |
| Evidence validity | 100% | 100% | met |

\* Scored only over anomalies that already name the injected service — which isolates the ranker from the grouper, but excludes the cases where the detector surfaced a downstream symptom instead. Those are the hard ones, so this is an upper bound, not an unconditional accuracy.

### The same scenarios without that filter

| Metric | Naming the injected service | Any anomaly in the window |
| --- | --- | --- |
| Top-1 | 78% | 33% |
| Top-3 | 89% | 56% |
| MRR | 0.81 | 0.43 |
| Scenarios scored | 9 | 9 |

The right-hand column takes the first anomaly in each fault window whatever it names, so it includes the cascades. The gap between the columns is how much of the headline depends on the grouper having named the right service first.

## Per scenario

| Fault | Injected into | Ranked | RR | Evidence |
| --- | --- | --- | --- | --- |
| `service_crash` | `ci-deployer` | **ci-deployer** → ci-artifacts → ci-pipeline | 1.00 | 2/2 |
| `service_crash` | `ci-runner` | **ci-runner** → ci-pipeline → ci-scheduler | 1.00 | 2/2 |
| `bad_deploy_latency` | `ci-scheduler` | **ci-scheduler** → ci-pipeline → ci-web | 1.00 | 3/3 |
| `dependency_timeout` | `ci-runner` | ci-scheduler → ci-artifacts → ci-pipeline | 0.00 | 3/3 |
| `config_error` | `ci-pipeline` | ci-web → ci-scheduler → **ci-pipeline** | 0.33 | 2/2 |
| `memory_exhaustion` | `ci-artifacts` | **ci-artifacts** → ci-pipeline → ci-web | 1.00 | 2/2 |
| `resource_exhaustion` | `ci-pipeline` | **ci-pipeline** → ci-web → ci-scheduler | 1.00 | 3/3 |
| `app_fault` | `ci-runner` | **ci-runner** → ci-pipeline → ci-scheduler | 1.00 | 1/1 |
| `app_fault` | `ci-runner` | **ci-runner** → ci-pipeline → ci-scheduler | 1.00 | 1/1 |

