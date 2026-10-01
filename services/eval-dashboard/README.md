# eval-dashboard

One page for the whole loop: inject a labelled fault, decide what to do about the
diagnosis, read the history of both, and see the rate at which faults are caught.

Four tabs:

| Tab | What it does | Where the data comes from |
| --- | --- | --- |
| **Inject a fault** | Pick a fault class, service and duration, set the class's own parameters, fire it | `POST /faults` on the fault injector |
| **Decisions** | Approve, reject or request more information on an incident awaiting a human | `GET /incidents`, `POST /incidents/{id}/approve\|reject\|request-info` on the orchestrator |
| **History** | One row per injected fault: when it started, whether anything detected it and how fast, what the ranker blamed, what a human decided | TimescaleDB — `fault_scenarios` joined to `anomalies`, `hypotheses` and `incidents` |
| **Detection rate** | Injected vs detected over time, unattributed alerts, and detection rate per fault class | the same join, bucketed with `time_bucket` |

## Running it

```bash
# against a stack already up, from this directory
pip install -r requirements.txt
python main.py                      # http://localhost:5010
```

Defaults point at the host-published ports, so no configuration is needed when the
stack is running under Compose on the same machine. Override with `PG_HOST`,
`FAULT_INJECTOR_URL`, `ORCHESTRATOR_URL`, `DIAGNOSIS_URL`, `LOAD_GENERATOR_URL`
and `PORT`.

To run it inside Compose instead, add this to `docker-compose.yml` — it is not
there yet, because `docker-compose.yml` is shared and this service is new:

```yaml
  eval-dashboard:
    build: ./services/eval-dashboard
    restart: unless-stopped
    environment:
      PG_HOST: timescaledb
      FAULT_INJECTOR_URL: http://fault-injector:5001
      ORCHESTRATOR_URL: http://orchestrator:8090
      DIAGNOSIS_URL: http://diagnosis-service:8000
      LOAD_GENERATOR_URL: http://load-generator:5002
    ports:
      - "5010:5010"
    depends_on:
      timescaledb:
        condition: service_healthy
    networks:
      - diagnosis-net
```

## Two decisions worth knowing

**It is a server, not a static page.** None of the services set CORS headers, so a
browser cannot call the fault injector and the orchestrator directly from a page
served anywhere else. Every call is proxied here, which also keeps the database
password out of the browser.

**It decides nothing.** The Decisions tab posts to M4's orchestrator and renders
what comes back; the state machine, the audit chain and the executor stay there.
This is a second *view* onto the decision path, not a second copy of it — M4's
console on `:3000` remains the reference implementation, and anything this page
appears to decide differently is a bug here.

## Where the numbers agree with the report

"Detected" means the same thing as in the evaluation report: an anomaly naming the
injected service, inside the fault window plus a 90-second grace. The window is
capped at 10 minutes so a fault whose recovery was never recorded cannot swallow
every later anomaly.

Alerts raised while no fault was running are labelled **unattributed**, not false
positives. The testbed degrades on its own (issue #33), so some of them are real
problems nobody injected. Calling them false here would bake a measurement error
into the console; that argument belongs in the evaluation report, which has the
space to make it.

## State of testing

Verified against the live stack on 2026-10-01: all five backends reachable, all
four SQL queries run, and a fault injected through the Inject tab appeared in
History 24.1 seconds later. Per-class detection rates match the evaluation report
exactly (`bad_deploy_latency` 6/6, `service_crash` 6/6, `db_pool_saturation` 0/2).

Two bugs the live run found, both fixed:

- The history query joined `incidents`, which is M3's past-postmortem RAG corpus
  and has no `state` column. M4's state machine is `orchestrator_incidents` — the
  comment at the top of `008_incidents.sql` warns about exactly this collision.
- `EXTRACT` and `percentile_cont` return `numeric`, psycopg2 maps that to
  `Decimal`, and Flask serialises `Decimal` as a JSON *string*. A detection
  latency arrived in the browser as `"31.123"` and the first `.toFixed()` on it
  would have thrown. `jsonable()` now converts.

One thing the dashboard surfaced rather than caused: a fault detected on
2026-10-01 produced no hypotheses and no incident, and the orchestrator's newest
incident was still from 23 September. Its Kafka consumer thread had died on
30 September with `KafkaTimeoutError: Unable to bootstrap from kafka:9092`, which
`_connect()` does not catch - it retries `NoBrokersAvailable`, aliased to
`KafkaConnectionError`, and `KafkaTimeoutError` inherits from `RetriableError`
instead. The thread exits, the HTTP API stays healthy, and nothing says so. The
consumer group had 101 messages of lag and no members.

Reviving it needed a group reset to `latest` before the restart, so it skipped
the backlog rather than opening 101 incidents at once - which is what the
consumer's own comment says should happen on a restart. The underlying bug is
still there and will recur on any cold start where Kafka is slower to come up
than the orchestrator.

The remedy columns were then verified end to end: a catalogue latency fault
detected in 31s, `rollback_deploy:dep-2026-10-01-0612` proposed, approved through
the Decisions tab, and the whole chain visible in one History row - with the
fault ending because the injector withdrew it, not because the action ran.
