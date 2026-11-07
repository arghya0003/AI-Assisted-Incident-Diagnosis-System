# Attribution evaluation

Generated 2026-11-07T12:01:34+00:00 · scenarios recorded since 2026-11-07T10:01:34+00:00

**9 of 9 scenario(s) scored.** A scenario is scored only if the detector raised an anomaly for it and the ranker returned hypotheses; one that never reached the ranker measures detection, not attribution, and counting it here would charge the ranker for a detection miss.

| Metric | Measured | Target | Verdict |
| --- | --- | --- | --- |
| Root-cause accuracy (Top-1)* | 78% | — | — |
| Root-cause accuracy (Top-3)* | 100% | Top-3 above 70% | met |
| Ranking quality (MRR)* | 0.87 | MRR above 0.6 | met |
| Evidence validity | 100% | 100% | met |

\* Scored only over anomalies that already name the injected service — which isolates the ranker from the grouper, but excludes the cases where the detector surfaced a downstream symptom instead. Those are the hard ones, so this is an upper bound, not an unconditional accuracy.

### The same scenarios without that filter

| Metric | Naming the injected service | Any anomaly in the window |
| --- | --- | --- |
| Top-1 | 78% | 33% |
| Top-3 | 100% | 67% |
| MRR | 0.87 | 0.48 |
| Scenarios scored | 9 | 9 |

The right-hand column takes the first anomaly in each fault window whatever it names, so it includes the cascades. The gap between the columns is how much of the headline depends on the grouper having named the right service first.

## Per scenario

| Fault | Injected into | Ranked | RR | Evidence |
| --- | --- | --- | --- | --- |
| `service_crash` | `stream-storage` | **stream-storage** → stream-cache → stream-playback | 1.00 | 5/5 |
| `service_crash` | `stream-catalog` | **stream-catalog** → stream-web → stream-playback | 1.00 | 2/2 |
| `bad_deploy_latency` | `stream-playback` | **stream-playback** → stream-cache → stream-web | 1.00 | 4/4 |
| `dependency_timeout` | `stream-cache` | stream-playback → stream-web → **stream-cache** | 0.33 | 4/4 |
| `config_error` | `stream-playback` | stream-web → **stream-playback** → stream-catalog | 0.50 | 2/2 |
| `memory_exhaustion` | `stream-storage` | **stream-storage** → stream-playback → stream-cache | 1.00 | 3/3 |
| `resource_exhaustion` | `stream-playback` | **stream-playback** → stream-web → stream-cache | 1.00 | 2/2 |
| `app_fault` | `stream-storage` | **stream-storage** → stream-cache → stream-playback | 1.00 | 2/2 |
| `app_fault` | `stream-cache` | **stream-cache** → stream-playback → stream-web | 1.00 | 1/1 |

