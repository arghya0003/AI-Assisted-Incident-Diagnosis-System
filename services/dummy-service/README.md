# dummy-service

One small service, configured by environment, that plays every part in the dummy
test sites. The sites exist to try the detector and the diagnosis layer on
something they were not tuned on, so only the signals have to be realistic.

## StreamBox, the streaming site

    docker compose -f docker-compose.yml -f docker-compose.sites.yml --profile streaming up -d

The player is at <http://localhost:8100>, or use `InciStreamUp` and `InciStream`.
`stream-web` calls `stream-catalog`, then `stream-playback` -> `stream-cache` ->
`stream-storage`, with `stream-traffic` sending 5 req/s. `stream-web` is the only
entry point.

## BuildBoard, the CI/CD site

    docker compose -f docker-compose.yml -f docker-compose.sites.yml --profile cicd up -d

The board is at <http://localhost:8101>, or use `InciCIUp` and `InciCI`. `ci-web`
calls `ci-pipeline`, which calls `ci-scheduler` -> `ci-runner`, then `ci-artifacts`
-> `ci-deployer`, with `ci-traffic` sending 5 req/s. `ci-runner` has two slots, so
when it slows down, requests queue for one and the wait shows up as latency.

Both sites can run together, and a plain `docker compose up -d` leaves their metrics
alone: `metrics-bridge` lists both jobs and only reads targets that are up.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `SERVICE_NAME` | `dummy` | name reported in responses |
| `DOWNSTREAM` | none | `host:port` list, called in order on every request |
| `WORK_MS` | `2` | CPU burned per request, so a CPU throttle has an effect |
| `LATENCY_MS`, `JITTER_MS` | `5`, `2` | simulated wait per request |
| `ERROR_RATE` | `0` | share of requests answered with a 500 |
| `DOWNSTREAM_TIMEOUT_S` | `2` | how long to wait for a downstream call |
| `BALLAST_MB` | `0` | memory held from startup, so `memory_exhaustion` has something to squeeze |
| `SLOTS` | `0` | requests served at once, `0` for unlimited; others wait up to `DOWNSTREAM_TIMEOUT_S` |
| `PAGE` | `player.html` | page served at `/` |
| `ADMIN_TOKEN` | none | turns on `/admin/fault`; compose sets it to `dummy-admin` |

Tests: `python -m pytest -q`. One test is Linux-only, because process metrics read `/proc`.

## Application faults

`app_fault` makes a service misbehave from the inside, in the site's own terms. The
injector sets it through `/admin/fault`; the service ends it on its own after `ttl_s`,
so a fault left by a dead injector is gone shortly after it should have ended.

| Mode | Service | What happens |
| --- | --- | --- |
| `slow_storage` | `stream-storage` | +400 ms on every request, held |
| `cache_flush` | `stream-cache` | +150 ms that fades to nothing over the fault, as a cache warms |
| `stuck_runner` | `ci-runner` | 60 ms -> 600 ms; two slots cannot keep up with 5 req/s, so requests queue |
| `flaky_build` | `ci-runner`, `ci-deployer` | 30% of requests fail with a 500 |

What the first live run showed (2026-11-03):

- `stuck_runner` saturates within seconds: the page went to 504s almost at once and stayed
  there. It is a step, not a ramp. A milder `extra_latency_ms` near 400 should build more
  slowly; that has not been tried.
- `cache_flush` fades as designed for requests (the page's load time fell from ~250 ms to
  ~100 ms over the fault), but the `latency_p95_ms` metric shows a plateau and then a drop.
  A one-minute p95 reports the slowest requests in its window, which are the earliest ones,
  so it lags the fade by about a minute. The fade is only visible in the p50.
- Histogram buckets cap what p95 can show: `ci-runner`'s p95 read ~4.4 s during
  `stuck_runner` although no request can take more than ~2.6 s, because everything above
  2.5 s lands in one bucket (2.5 s to 5 s).

    curl -X POST localhost:5001/faults -H 'Content-Type: application/json' \
         -d '{"fault_type":"app_fault","service":"ci-runner","mode":"stuck_runner","duration_s":120}'

The `/admin` token is a guard against accidents. It is a default in a public repository,
so it is not a secret, and these services are test targets, not something to expose.

## Evaluation suites

    docker compose run --rm evaluation-runner live --suite streaming
    docker compose run --rm evaluation-runner live --suite cicd

`streaming-smoke` and `cicd-smoke` are two-scenario wiring checks. Each full suite is every
fault class that applies to the site (`db_pool_saturation` is MySQL-specific) plus its app
faults, run once as the held-out result: the detector and ranker were only tuned on Sock
Shop, so nothing is adjusted before it. Anything run after a change is **tuned**, and a
report should say so.

- The gap after each fault is five minutes, not 2.5. The `config_error` miss on 2026-11-01
  came 2.5 minutes after another fault, with the baseline unsettled.
- The runner refuses to start unless every service the suite touches reported request
  metrics in the last two minutes. A site that is down would otherwise be faulted and
  scored as a string of misses.
- Only the site's own alerts are scored. Sock Shop keeps running beside it, and its
  alerts say nothing about the detector on the site; the report states how many were
  set aside.

## Known gaps

- The injector and dashboard always offer both sites' services. With a site off, the
  runner refuses its suite, but a fault injected by hand is accepted and then recorded
  as failed.
- Stopping a site raises a `liveness` alert, because silence looks like an outage.
