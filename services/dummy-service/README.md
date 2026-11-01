# dummy-service

One small service, configured by environment, that plays every part in the dummy
test sites. The sites exist to try the detector and the diagnosis layer on
something they were not tuned on, so only the signals have to be realistic.

## StreamBox, the streaming site

    docker compose -f docker-compose.yml -f docker-compose.sites.yml --profile streaming up -d

The player is at <http://localhost:8100>. Calls run `stream-web` ->
`stream-playback` -> `stream-storage`, with `stream-traffic` sending 5 req/s.

Always include both `-f` files while the site is up. `docker-compose.sites.yml`
adds `streaming-site` to the bridge's `SERVICE_JOBS`, so a plain
`docker compose up -d` recreates the bridge without it and the site's metrics
stop.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `SERVICE_NAME` | `dummy` | name reported in responses |
| `DOWNSTREAM` | none | `host:port` list, called in order on every request |
| `WORK_MS` | `2` | CPU burned per request, so a CPU throttle has an effect |
| `LATENCY_MS`, `JITTER_MS` | `5`, `2` | simulated wait per request |
| `ERROR_RATE` | `0` | share of requests answered with a 500 |
| `DOWNSTREAM_TIMEOUT_S` | `2` | how long to wait for a downstream call |

Tests: `python -m pytest -q`. One test is Linux-only, because process metrics read `/proc`.

## Known gaps

- The dependency graph has no `stream-` services yet, so the ranker cannot follow
  the cascade.
- The injector does not accept `stream-` services yet.
- `memory_exhaustion` cannot kill these services: they use about 29 MiB and the
  injector's lowest cap is 48 MB.
- The grouper can merge an alert from this site with an unrelated Sock Shop alert
  raised at the same time.
- Stopping the site raises a `liveness` alert, because silence looks like an outage.
