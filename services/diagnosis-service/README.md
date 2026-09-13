# diagnosis-service (Member 3)

Given an `anomaly_id`, returns a ranked, evidence-cited list of root-cause hypotheses with a
proposed remediation from a fixed action vocabulary. Nothing is ever executed — output is for
human approval via M4. Scores candidates deterministically (deploy proximity, dependency-graph
position, co-anomaly, incident similarity), then uses a local LLM only to explain and re-rank,
and drops any hypothesis citing evidence that was not provided to it.

Build spec and phase status: [PLAN.md](PLAN.md).

**Status:** Phase 0 complete. No service code yet — Phase 1 adds the runnable skeleton and the
"how to run standalone" section.

---

## Phase 0 results — 2026-09-13

Reproduce with:

```
bash   services/diagnosis-service/scripts/phase0_stack_check.sh
python services/diagnosis-service/scripts/phase0_llm_bench.py [--num-ctx 8192]
```

### Host

| Item | Value |
| --- | --- |
| GPU | NVIDIA GeForce RTX 3050 Ti Laptop, 4096 MiB (about 3295 MiB free before any model loads — Windows holds the rest) |
| RAM | 15.2 GB |
| Free disk | 49 GB |
| Ollama | 0.34.0, `OLLAMA_HOST=0.0.0.0` |
| Docker | 29.5.2, Compose v5.1.3 |

### Upstream stack — `phase0_stack_check.sh`: 18 pass, 0 fail, 0 warn

| Check | Result |
| --- | --- |
| M1 stack | 24 containers running, none restarting or failed |
| `metrics` | 34,565 rows, newest sample 4 s old — pipeline live |
| `deploys` | 55 rows |
| `evidence` | exists, 0 rows (expected — M3 is its writer) |
| `fault_scenarios` | exists, 0 rows — no faults injected on this volume yet |
| pgvector | **available** in the TimescaleDB image |
| `metrics.raw`, `deploys.events` | exist, 3 partitions each |
| `anomalies.detected` | exists, **1 partition** — auto-created on M2's first publish, not by `kafka-init` |
| M2 output | real anomalies present on the topic |
| Ollama from a container | reachable via `host.docker.internal:11434` |

### LLM — `phase0_llm_bench.py`, 10 JSON-mode runs on a Phase 6-shaped prompt

| Metric | `num_ctx` 8192 | `num_ctx` 4096 |
| --- | --- | --- |
| Valid, schema-conforming JSON | **10/10** | 10/10 |
| Runs citing an evidence ID not in the prompt | **0/10** | 0/10 |
| Latency p50 (min / max) | **5.0 s** (4.4 / 5.4) | 8.5 s (7.8 / 30.2) |
| Generation speed | 24.7 tok/s | 32.9 tok/s |
| Resident size | 4.2 GB | 3.6 GB |
| Placement | **44% CPU / 56% GPU** | 36% CPU / 64% GPU |
| `nomic-embed-text` | 768 dims, about 0.9 s warm (27.5 s cold) | — |

**Reading:**

- `phi4-mini` is a defensible choice on this hardware for correctness: JSON compliance and
  citation discipline were perfect on this prompt. Ten runs is not a guarantee; the Phase 6
  retry loop and Phase 7 guardrail stay.
- It does **not** fit entirely on the GPU at either context size. At 8K, 19 of 33 layers are
  offloaded to the GPU and the rest run on CPU; the KV cache alone is 1 GB. Dropping to 4K
  moves only a few more layers across and did not reduce latency in this run (the 30.2 s max
  includes a model reload). **Keep `num_ctx` at 8192** — it buys prompt room without a
  measured latency cost.
- About 5 s per `/analyze` is acceptable for a human-approval workflow. It is a cost for the
  Week 8-10 evaluation runs, which is why Phase 8 caches responses.
- The first model load crashed Ollama's runner once (`0xc0000409`) and returned HTTP 500; the
  immediate retry loaded normally. Phase 6's client must treat a 500 on the first call as
  retryable, not fatal.
- The benchmark prompt is a stand-in written for Phase 0, not the final Phase 6 prompt. If
  results diverge later, suspect the prompt before the hardware.
