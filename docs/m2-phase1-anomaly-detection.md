# M2 Phase 1 Notes: Anomaly Detection

Builds on the EWMA z-score detector that already existed in
`services/anomaly-detector/main.py`. This pass fills in the parts of M2's
plan that weren't there yet: a comparison baseline, dedup/grouping,
deploy-window suppression, and persistence for the evaluation runner
(Phase 2) to read.

## Comparison baseline: static threshold
`StaticThresholdDetector` learns a plain mean/stdev over the first
`warmup` samples, then freezes `threshold = mean + k*stdev` forever - it
never re-adapts. This is deliberately naive: the point is to give
`eval-runner` something to score EWMA against, per the plan ("add at
least one comparison baseline ... so the evaluation can show that EWMA
actually earned its place"). CUSUM (the plan's other suggested option)
was left out - one working comparison baseline is what the plan requires,
and a static frozen threshold is the sharpest contrast to
"continuously-adaptive," which is the property the evaluation is actually
trying to demonstrate.

Both detectors run side by side, per `(service, metric)`, off the same
`metrics.raw` stream.

## Dedup and grouping
Previously: one `anomalies.detected` message per `(service, metric)` that
tripped - exactly what the plan warns against ("a single failure will trip
fifteen metrics across six services; emit *one* anomaly event"). Now, raw
per-metric detections are buffered and flushed every
`FLUSH_INTERVAL_SECONDS` (default 5s, matching `metrics-bridge`'s scrape
interval) into a single grouped record: `services[]`/`metrics[]` deduped
and sorted, `severity` = the highest severity in the group,
`t_onset` = the earliest onset in the group. `group_detections()` in
`main.py` is a pure function - no Kafka/Postgres - so it's unit-tested
directly (`tests/test_detector.py`).

## Deploy-window suppression
The detector now also consumes `deploys.events` (a second, independent
Kafka consumer, its own consumer group, `latest` offset - it only cares
about deploys from now on) and tracks each service's most recent deploy
time. A detection for a service is suppressed if it lands within
`DEPLOY_SETTLE_WINDOW_SECONDS` (default 8s) of that service's last deploy.

This is a **settle window, not a baseline reset**: it's meant to absorb a
brief, benign blip right at deploy time (connection warm-up, JIT warm-up,
cache misses), not to hide a real regression that happens to start at
deploy time. A fault that persists past the settle window - such as the
`bad_deploy_latency` fault scenario, whose CPU throttle runs far longer
than 8s - still fires normally. The honest cost: that fault type's
measured detection latency in the evaluation report will include (roughly)
the settle window itself, since any detection attempt inside it is
discarded rather than just delayed. `eval-runner`'s numbers should show
this rather than hide it.

## Explicitly not implemented: seasonality suppression
The plan groups this with deploy-window suppression ("deploy-window /
seasonality suppression"). Seasonality suppression exists to stop a
detector from flagging a normal, recurring daily/weekly traffic pattern as
an anomaly. This testbed has no real diurnal or weekly traffic - it's a
short-lived local Docker Compose stack with synthetic/generated load - so
there is no real seasonal signal to suppress against, and building a fake
one wouldn't be validated by anything. Documented gap, not a silent one:
revisit if the testbed is ever run continuously long enough to have a real
pattern.

## Persistence
Both detectors' grouped anomalies are written to the new `anomalies` table
(`timescaledb/init/005_anomalies.sql`), tagged by `detector`
(`ewma` / `static_threshold`), including the raw per-metric detections
that went into the group (`detail` JSONB column) for debugging. Only the
`ewma` rows are also published to `anomalies.detected` on Kafka - that's
the one contract M4 builds against (CONTRACTS.md); `static_threshold` is
an evaluation-only signal, not a second "official" alert stream.

## Verification
Unit-tested directly against the pure detector/grouping/suppression logic
(`services/anomaly-detector/tests/test_detector.py`, 6 tests, all green):
EWMA fires on a step change and stays quiet on stable noise; the static
detector freezes its threshold after warmup and still fires on a large
spike; `group_detections` dedupes and picks max-severity/earliest-onset
correctly; the settle-window check is true only inside the window.

**Not verified live end-to-end** (running the full stack against real
Sock Shop traffic, the way M1's phase docs did) - this pass was authored
in an environment with no Docker daemon available, so `docker compose up`
against the real testbed wasn't possible here. Before relying on this for
the evaluation report, run it against the real stack:
`docker compose up -d --build`, generate some traffic, and confirm rows
land in `anomalies` (`select detector, count(*) from anomalies group by
detector;` via `adminer` on :8082) and on the `anomalies.detected` topic
(via `kafka-ui` on :8081).
