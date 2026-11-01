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

Tests: `python -m pytest -q`. One test is Linux-only, because process metrics read `/proc`.

## Known gaps

- The injector and dashboard always offer `stream-` services. With the site off,
  a fault is still accepted, then recorded as failed.
- The injector's `offered_rps_at_inject` reads Sock Shop's load generator, so on
  this site it says nothing about `stream-traffic`.
- Stopping the site raises a `liveness` alert, because silence looks like an outage.
