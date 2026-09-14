# Phase 9 Notes: Anomaly Detection & Evaluation (M2)

M2's slice: "something is wrong" — and "how do we know we're right". Builds on M1's
`metrics.raw` stream and fault-injection harness, and feeds M4 via `anomalies.detected`.

## What was already here, and what this replaces

A first-pass EWMA detector existed: a single `main.py` that kept a per-(service, metric)
EWMA baseline and emitted one event per breach. It worked, but it could not support the
results this project has to defend — there was nothing to compare EWMA against, one fault
produced a dozen alerts, and nothing measured any of it. This phase rebuilds it into four
modules and adds the evaluation harness.

## The blind spot the first live run exposed

The first evaluation run against the real testbed detected a CPU-throttle fault on
`catalogue` in 28s and **missed a `service_crash` entirely** — zero alerts. The metrics
table explained it: a 50-second hole where `payment` should have been.

```
 2026-09-13 13:18:02+00 |       6
 2026-09-13 13:18:52+00 |       6     <- 50s gap, nothing in between
```

`metrics-bridge` asks Prometheus for targets whose health is `up` and skips the rest, so a
stopped container does not report bad numbers — it stops reporting at all. Every detector
in `detectors.py` judges a sample that arrived, which makes all four of them structurally
blind to a hard outage. No threshold tuning would have fixed this; the data simply is not
there. And "the service is completely down" is arguably the most important incident class
there is.

`staleness.py` closes it. A service that was reporting regularly and then goes quiet is
itself a high-severity signal. The monitor is driven by the clock rather than by arriving
samples, because the trigger is an absence, and it dates the outage from when the data
stopped rather than when the timeout expired — so `t_onset` stays honest. Re-running the
same suite afterwards detected both faults, `service_crash` in 32.9s.

This is the clearest argument for why the evaluation harness had to be built before the
detector was declared finished. The detector looked correct, passed its unit tests, and was
silently incapable of noticing a dead service.

## Two bugs found in the original detector

Both were found by writing tests for behaviour rather than by reading the code, and both
are regression-tested now.

**Error-rate anomalies could never fire.** The z-score used a fixed noise floor,
`baseline_std = max(prev_std, 0.5)`. `error_rate` is a ratio in [0, 1], so a 3-sigma
breach needed an error rate above 1.5 — unreachable. The service would have run forever,
looking healthy, silently blind to every error-rate anomaly. The floor is now relative to
the baseline mean (`detectors._EWMABaseline.std`), so it scales with whatever the metric's
natural magnitude happens to be.

**The baseline absorbed ongoing faults.** Breaching samples were folded straight into the
EWMA, so a sustained fault dragged the baseline up behind it and the detector went quiet
mid-incident — exactly when it was most needed. Breaching samples no longer update the
baseline, with a `max_frozen` cap so a genuine permanent level shift is eventually accepted
rather than alerted on forever.

## Two more found by a live run after a restart (2026-09-14)

A smoke run straight after a Docker restart missed a `bad_deploy_latency` fault that
plainly worked — catalogue's p95 went from 4.75 ms to 362 ms and the detector logged
the signals — yet no alert was emitted. Two bugs compounded:

**Latency getting better was an anomaly.** After a restart, catalogue was slow for its
first minute while it booted. The detector learned that ~39 ms as its baseline, and when
latency settled back to its real ~5 ms the two-sided z-score read the drop as a huge
deviation and raised a high-severity alert. Latency and error rate are now
upward-only (`detectors.UPWARD_ONLY_METRICS`): a drop is not a breach, so it is
observed and the baseline follows the metric down. CUSUM also stops keeping its downward
accumulator for these metrics, since a banked improvement would otherwise be added to
the next small rise. `cpu_rate` and `memory_bytes` stay two-sided — a sudden fall there
usually means the process restarted.

**A small alert could mute a big one.** That false alarm put catalogue into its 120s
cooldown, and every further blip sample extended the mute. When the real fault arrived
two minutes later it was absorbed as "the same incident continuing". A cooling service
now remembers the peak score of the incident that muted it, and a signal
`ESCALATION_FACTOR` (3x) worse opens a new incident — here the fault scored 18.6
against a peak of 2.6. The escalated incident sets the new bar, so an ongoing fault
still does not re-alert every cycle.

Both are regression-tested, including an end-to-end replay test that rebuilds the exact
boot → settle → fault sequence. Replaying that morning's recorded metrics through the
fixed code detects the missed fault in 38.2s with no false alarm.

Worth recording why the harness scored this run as "0 false positives": the startup false
alarm fired during the runner's warm-up, before scoring began. The run reported a clean
false-positive rate while a real false alarm had fired — a reminder to read the detector
log, not just the report.

## Detectors, and why there is more than one

`detectors.py` holds four implementations behind one interface, selected with the
`DETECTOR` env var:

| Detector | Statistic | Why it is in the set |
| --- | --- | --- |
| `ewma` | EWMA mean/variance, z-score | The primary detector this project argues for. |
| `static` | Hand-configured per-metric limits | The honest baseline EWMA has to beat. |
| `zscore` | Rolling-window 3-sigma | The textbook comparison. |
| `cusum` | Two-sided tabular CUSUM | Catches slow drift a per-sample test never trips on. |

Alongside these runs `staleness.py`, which is not a detector in the same sense — it is not
selectable, it always runs, and it watches for the absence of data rather than the shape of
it. See the section above for why.

The static thresholds are what an engineer would plausibly hand-write (p95 > 500 ms,
error rate > 5%, and so on). Picking absurd values would make the ablation meaningless,
so they are deliberately reasonable.

The shared base class owns warm-up, corroboration and baseline freezing so those cannot
quietly become confounds — if EWMA won only because it had a different warm-up, the result
would be worthless. `static` is exempt from warm-up because it has no baseline to learn,
and forcing one would hand EWMA an unearned latency advantage.

**Statistical vs practical significance.** A metric resting at zero has almost no variance,
so a microscopic wobble scores an enormous z. Each metric therefore also carries a minimum
deviation worth alerting on (`MIN_DEVIATION`) — an error rate has to move a full percentage
point, p95 latency 20 ms. Without this the detector storms on healthy near-constant metrics.

**A CUSUM cannot be double-gated.** Requiring N consecutive breaches on top of a CUSUM
destroys it: each threshold crossing clears the accumulator, so the evidence it spent
dozens of samples gathering is thrown away and it can never fire. This was a real bug
caught by a test — CUSUM crossed its threshold repeatedly and emitted nothing. The reset
now happens only when a signal is actually emitted (`_on_fire`), and CUSUM defaults to
`required_breaches=1` because its statistic already encodes persistence.

## Grouping: one incident, not fifteen alerts

A CPU-throttled `catalogue` trips p50/p95/p99 latency and error rate on `catalogue`, then
the same on `front-end` as timeouts propagate. `grouping.py` collapses these into one event
with a `services[]` / `metrics[]` member list, plus `contributors[]` carrying the per-metric
detail M3 needs.

Two timing decisions:

- An event is emitted `group_delay` (15s) after the group's **first** signal, not after its
  last. Waiting for the fault to go quiet would make a 300s fault alert 300s late.
- `t_detected` is stamped from the first contributing signal, not from emission time, so the
  grouping delay does not inflate the detection latency the evaluation measures.

A per-service cooldown (120s) stops an ongoing fault from re-alerting every cycle, while a
different service can still raise its own incident during that window.

**Known limitation:** grouping is purely time-based, so two genuinely unrelated faults
overlapping in time would merge into a single event. For the evaluation harness, which
injects one fault at a time, this is correct behaviour; for production it would need
dependency-graph-aware grouping. Documented rather than hidden — `services[]` is a list the
consumer can inspect.

## Deploy windows raise the bar, they never close the gate

The obvious implementation — mute a service while it is mid-deploy — is wrong here, and
badly so. `bad_deploy_latency` is the single most important fault class in this project:
a deploy that genuinely breaks a service. Hard suppression would mute precisely the
incidents the system exists to catch, and would do it silently.

Instead, a service inside a deploy window must clear a higher evidence bar (4 consecutive
breaches instead of 2). A cold-start blip lasting a sample or two is filtered; a real
regression that persists still fires, and arrives tagged with the `deploy_id` M3 needs for
correlation. There is a test asserting the window can only ever raise the bar.

**Seasonality suppression is deliberately not implemented.** The testbed runs synthetic
traffic with no diurnal or weekly cycle, so a time-of-day baseline would be modelling noise
and could not be validated. Building it would be untested ceremony. Recorded as a known gap.

## Evaluation harness

`services/evaluation-runner/` — two modes, because they answer different questions.

**`live`** injects the labelled fault suite against the running testbed and scores what the
deployed detector actually published to `anomalies.detected`. This is the honest end-to-end
measurement: real faults, real pipeline, real latency.

```
docker compose run --rm evaluation-runner live
docker compose run --rm evaluation-runner live --suite smoke   # quick wiring check
```

**`replay`** pulls a recorded window back out of TimescaleDB and runs every detector over
that identical stream offline.

```
docker compose run --rm evaluation-runner replay --since-minutes 120
```

Replay is where ablation numbers come from, and the reason is worth stating: running each
detector against its own live window would confound the comparison with whatever else the
testbed was doing at the time. Same input, different detector, or the comparison means
nothing. Replay is also deterministic — the grouper is driven by each sample's own
timestamp rather than the wall clock — so a given window always reproduces the same events.

Reports land in `./results/` as Markdown and CSV, plus a stable `latest.md`.

### Scoring decisions

- **Three outcomes, not two.** An alert that fires during a fault but names the wrong
  service is `misattributed`, not the same as `missed`. Collapsing them would flatter the
  detector — the whole point of the system is to say *which* service is at fault.
- **A 90s grace period after recovery.** Metrics derive from Prometheus `rate(...[1m])`
  windows, so a fault's effect decays for about a minute after it ends. Without the grace
  period, genuine late detections would be miscounted as false positives and the
  false-positive rate would be overstated.
- **Overlapping fault windows are merged** before computing quiet time, so back-to-back
  scenarios cannot discount the same second twice and shrink the FP-rate denominator.
- **`t_inject` comes from the database**, not the runner's clock — the injector records it
  when the fault actually starts.
- **Unmeasured metrics report "not measured", never 0.0.** Root-cause accuracy, MRR and
  evidence validity all score M3's ranker, which is not wired up yet. `mean_reciprocal_rank`
  returns `None` for an empty set rather than a confident zero that would read as a measured
  failure. The functions are implemented and tested, so they start producing numbers the
  moment M3 supplies a ranker.

### Settle time between scenarios

`--settle-seconds` defaults to 150s, which must stay above the grouper's 120s cooldown.
Inject faster than that and the next scenario is suppressed as a continuation of the
previous one, which would look like a detector miss rather than a harness artefact.

## Tests

93 tests, no Docker required: `python -m pytest tests` in either service directory.

`services/evaluation-runner/tests/test_replay.py` is the end-to-end one — it generates a
synthetic metric stream with a known fault and runs the real detector, real grouper and
real scoring code over it. It asserts every detector finds an obvious fault, that a healthy
stream produces zero alerts, that one fault yields one event rather than one per metric,
and that replay is reproducible.

Worth knowing when reading it: on synthetic data with a 120x latency spike, all four
detectors tie at 100% detection. That is expected — the fault is trivially large. The
interesting differences only appear on real testbed data with realistic noise, which is
what a `live` run plus a `replay` ablation is for.

## Results

Full seven-scenario suite against the live stack (2026-09-13):

| Fault type | Scenarios | Detected | Of observable | Missed | Unobservable | Median latency |
| --- | --- | --- | --- | --- | --- | --- |
| `bad_deploy_latency` | 3 | 1 (33%) | 100% | 0 | 2 | 32.9 s |
| `db_pool_saturation` | 1 | 0 (0%) | 0% | 1 | 0 | — |
| `service_crash` | 3 | 3 (100%) | 100% | 0 | 0 | 39.0 s |
| **overall** | **7** | **4 (57%)** | **80%** | **1** | **2** | **36.5 s** |

Median detection latency 36.5s against a 60s target, and **zero false positives across 11.9
minutes of quiet observation** against a target of under one per hour.

Both rates are quoted deliberately. 57% is over every scenario attempted and cannot be
gamed; 80% excludes scenarios where the target service emitted nothing to detect. Reporting
only the second would let a broken testbed pose as a good detector.

Detection latency varies by tens of seconds between runs — the same `bad_deploy_latency`
scenario measured 28.1s, 41.9s and 32.9s across three runs. The underlying metric is a
`rate(...[1m])` window sampled every 5s, so where a fault lands inside a scrape cycle moves
the result substantially. Quote medians from the full suite; a single scenario is one
sample, not a measurement.

## Ablation: learned baselines vs a fixed threshold

Replay over 26,370 recorded samples spanning 18 real injected scenarios. Every detector saw
byte-identical input, so the differences are attributable to the detector.

| Detector | Scenarios | Detected | Missed | Median latency | False positives/hour |
| --- | --- | --- | --- | --- | --- |
| `ewma` | 18 | 12 (67%) | 6 | 35.5 s | 0.00 |
| `cusum` | 18 | 12 (67%) | 6 | 35.5 s | 0.00 |
| `zscore` | 18 | 12 (67%) | 6 | 35.5 s | 0.00 |
| `static` | 18 | **8 (44%)** | 10 | 35.5 s | 0.00 |

**The static threshold missed every single latency fault.** Its 8 detections are exactly the
8 `service_crash` scenarios, which the staleness monitor catches regardless of which
detector is configured. On latency it scored zero.

The reason is worth quoting in the report, because it is the entire argument for a learned
baseline. During a CPU throttle, `catalogue`'s p95 went from a **5.9 ms** baseline to
**141 ms average, peaking at 222 ms** — a 38x regression, a service unambiguously in
trouble. The hand-configured threshold for `latency_p95_ms` is 500 ms, so it never fired.

And 500 ms is not a strawman. It is a defensible number for a global latency alert; setting
it at 20 ms to catch this incident would make it scream continuously on any service whose
healthy p95 is 200 ms. **No single fixed threshold can serve both services.** A baseline
learned per (service, metric) can, which is precisely what EWMA buys.

Two honest caveats:

- `ewma`, `cusum` and `zscore` tie exactly on this data. The result supports "a learned
  baseline beats a fixed threshold", **not** "EWMA beats other adaptive statistics" — the
  faults here are large step changes, which all three handle equally well. Distinguishing
  them would need the slow-drift faults (memory leak / OOM) the injector cannot yet produce.
- The static thresholds were chosen before any of these runs and deliberately set to
  plausible engineer-written values. Picking absurd ones would have made the comparison
  meaningless.

## Two testbed limitations the evaluation exposed

Neither is a detector failure, and both were confirmed against the database rather than
assumed. They are the most useful output of the harness so far.

### Four of seven services have no traffic to degrade

`carts`, `front-end`, `orders` and `shipping` emit only `cpu_rate` and `memory_bytes`.
Sock Shop runs without a load generator, so those services serve no requests; their
`request_duration_seconds` histograms are empty, `histogram_quantile` returns NaN and
metrics-bridge drops the sample. CPU-throttling an idle service changes nothing anyone can
measure — a latency fault there is undetectable by construction.

The same gap means **`error_rate` is never ingested for any service**: with no 5xx
responses the PromQL selector returns an empty vector rather than zero, so the sample is
skipped entirely. The relative-noise-floor fix described above is therefore correct but
currently untestable against live data.

This is why the harness classifies such scenarios `unobservable` rather than `missed`.

### `db_pool_saturation` starves connections nobody asks for

Raising the injector's cap from 100 to 160 made the fault genuinely work — it opened 152
connections and MySQL returned `1040 Too many connections`, so `catalogue-db`'s pool of 151
really was exhausted. Catalogue was completely unaffected regardless: p95 6.1ms, p99 7.2ms,
indistinguishable from baseline.

The fault exhausts the database's capacity to accept *new* connections, but `catalogue`
holds an already-established pool and, at 0.2 req/s, never needs to open another one. The
fault is real and lands on something the victim does not use.

Making it a genuine incident needs one of:

- enough traffic that catalogue's own pool comes under pressure (the load-generator gap
  again), or
- killing catalogue's existing connections (MySQL `KILL` on its threads) so it is forced to
  reconnect into an exhausted pool.

The second is surgical and needs no load generator, but it carries a real risk of leaving
catalogue unable to refill its pool at all — a state that may need a container restart and
would corrupt any run in progress. It touches M1's fault injector and the shared testbed,
so it is written up here for the team rather than changed unilaterally.

## What is not done

- **Three fault classes, not six.** The plan names memory leak / OOM, dependency timeout
  cascade and config error as well. The injector cannot produce those yet; the scenario
  suite lists only what physically exists rather than scenarios that quietly do nothing.
- **MRR / top-k / evidence validity are unscored** until M3 exists. The harness is ready.
- **No CI.** Tests run locally only.
