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

The server and the page are verified: it serves, and every endpoint returns a
readable error rather than a stack trace when a backend is down. The four SQL
queries have **not** been run against a live TimescaleDB yet — Docker was stopped
when this was written. Run it against the stack before relying on the History and
Detection-rate tabs.
