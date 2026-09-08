# M2 Phase 2 Notes: Evaluation Runner

The evaluation harness the plan asks M2 to own: inject the fault
scenarios M1's `fault-injector` already knows how to run, and turn what
happens into the numbers the report needs - detection latency and
false-positive rate per fault type (the plan's explicit "proof it works"
for this slice), regenerable with a single command.

## Why this didn't need its own fault injector
M1 pulled `services/fault-injector/` forward (Phase 8) precisely so M2
wouldn't have to build fault injection from scratch: it already runs real,
physically-enforced faults (`bad_deploy_latency`, `service_crash`,
`db_pool_saturation`) against live containers and records ground truth in
`fault_scenarios`, in the exact "eval hooks" shape CONTRACTS.md specifies.
`eval-runner` is a thin client on top of that plus the `anomalies` table -
it doesn't duplicate the fault mechanics.

## Method
1. **Quiet-period baseline.** Sample `QUIET_PERIOD_SECONDS` (default 60s)
   with nothing injected, count anomalies per detector in that window,
   convert to a per-hour rate. This is the plan's "false-positive rate
   ... over quiet periods" metric.
2. **Per-fault trials.** For each of the three fault types
   (`SCENARIOS` in `main.py`), `TRIALS_PER_FAULT` times (default 3):
   `POST /faults` to fault-injector, poll `GET /faults` until the scenario
   reports `recovered` or `failed`, then look for the first anomaly (per
   detector) naming the ground-truth service with `t_detected` between
   `t_inject` and `t_inject + DETECTION_TIMEOUT_SECONDS` (default 90s). A
   trial with no match by the timeout counts as a miss.
3. **Aggregate** per `(fault_type, detector)`: trial count, hit count,
   recall (hits/trials), median and max detection latency over the hits,
   and the quiet-period false-positive rate. Raw per-trial rows and the
   aggregated table are both written as CSV
   (`eval-results/eval_raw.csv`, `eval-results/eval_summary.csv`) and the
   aggregated table is printed to stdout.

## Running it
Not part of the always-on stack - it's a one-shot job, gated behind a
Compose profile so `docker compose up` doesn't accidentally start
injecting faults into a stack someone's just trying to look at:

```
docker compose up -d --build          # bring the full stack up first
docker compose --profile eval run --rm eval-runner
```

Results land in `./eval-results/` on the host (bind-mounted). Tunable via
env vars on the `eval-runner` service: `TRIALS_PER_FAULT`,
`DETECTION_TIMEOUT_SECONDS`, `QUIET_PERIOD_SECONDS`.

## What this deliberately does not measure
Section 06 of the project plan's metrics table also lists **root-cause
accuracy** (top-1/top-3 hit rate) and **ranking quality** (MRR) - both
score M3's hypothesis ranking against ground truth, not M2's detection.
M3 doesn't exist in this repo yet. `fault_scenarios` (ground truth) and
`anomalies` (M2's output) already carry everything a future M3 evaluation
pass will need to join against; there's nothing to score on the ranking
side until M3's `/analyze` endpoint exists to call. Wiring that up belongs
to whoever builds M3's evaluation, not to this file.

The EWMA-vs-static-threshold ablation the plan calls for (Section 05,
"Where the Ablations Come From") **is** covered here: `eval_summary.csv`
reports both detectors' recall, latency and false-positive rate side by
side for exactly this comparison.

## Honesty note
This was authored in an environment without a Docker daemon available, so
the numbers above describe the method, not a captured run - there is no
`eval-results/eval_summary.csv` checked into the repo (and shouldn't be;
see `.gitignore`). Run the two commands above against the real stack to
get real numbers for the report. Expect `bad_deploy_latency`'s measured
latency to be inflated by roughly the deploy-window settle delay
(`docs/m2-phase1-anomaly-detection.md`) - that's a real, explainable cost,
not a bug to chase.
