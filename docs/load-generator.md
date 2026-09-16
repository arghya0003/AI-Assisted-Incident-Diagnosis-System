# Standing Load Generator (fix for issue #6)

## The problem it fixes
M3 injected a `bad_deploy_latency` fault against `catalogue` and nothing moved: p95 stayed
at 4.8 ms, `request_rate` at 0.2 req/s, `cpu_rate` at 0.0, before, during and after. The
harness worked — the scenario was recorded, throttled and recovered on schedule — but
there was no traffic for the throttle to slow down, so the fault produced no anomaly to
detect (M2) or diagnose (M3).

Three separate consequences, all from the same cause:

- **An idle system can't be evaluated.** Weeks 8–10 would have scored detection and
  diagnosis against runs where the faults were physically real but metrically invisible.
- **Latency baselines were meaningless.** At ~0.2 req/s, p95 comes from a handful of
  requests and jumps in histogram-bucket steps with no fault running at all (4.8 → 36.3 ms
  swings on catalogue).
- **4 of 7 services had no latency data at all.** `carts`, `orders`, `shipping` and
  `front-end` produced no `latency_p95_ms` samples, because nothing called them — there is
  no histogram to take a quantile of.

Phase 0 dropped Sock Shop's `user-sim` because it isn't part of the causal dependency
graph. That was right about the graph and wrong about the data plane: the graph doesn't
need traffic, but every metric in the pipeline does.

## What it does
`services/load-generator/` (port 5002) drives a steady, known load through `edge-router`,
following the whole call graph rather than one endpoint:

| Step | Path | Services exercised |
| --- | --- | --- |
| home | `GET /` | front-end |
| login | `GET /login` (basic auth, seeded `user`) | front-end → user |
| browse | `GET /catalogue`, `GET /catalogue/size`, `GET /catalogue/{id}` | → catalogue → catalogue-db |
| cart | `DELETE /cart`, `POST /cart`, `GET /cart` | → carts → carts-db |
| checkout (a fraction of journeys) | `POST /orders` | → orders → payment, shipping, user, carts; shipping → rabbitmq → queue-master |
| history | `GET /orders` | → orders → orders-db |

Checkout runs on `ORDER_PROBABILITY` of journeys (default 0.15) because it is the
expensive one and writes an `orders-db` row nothing cleans up. Setting it to 0 removes
payment and shipping from the traffic entirely.

## Configuration

| Env var | Default | Notes |
| --- | --- | --- |
| `TARGET_URL` | `http://edge-router` | Entry point; always go through the router, not a service directly |
| `WORKERS` | `4` | Concurrency cap |
| `TARGET_RPS` | `5` | Offered load across all workers |
| `ORDER_PROBABILITY` | `0.15` | Fraction of journeys that check out; 0 disables checkout |
| `REQUEST_TIMEOUT_SECONDS` | `10` | A throttled service can exceed this — that's the point |
| `CATALOGUE_REFRESH_SECONDS` | `300` | Item ids are read from the running catalogue, never hardcoded |
| `SHOP_USER` / `SHOP_PASSWORD` | `user` / `password` | Seeded Sock Shop customer (the `user-db` image ships it with an address and a card, which `POST /orders` needs) |

## Open loop, deliberately
Pacing is a shared rate limiter that hands out one request slot every `1/TARGET_RPS`
seconds, **not** a think-time pause after each response. A closed-loop generator reduces
its own offered load exactly when the system slows down — during a fault — which is the
one moment the load has to stay constant for the metrics to mean anything.

The limiter also refuses to bank credit for time it spent stalled: without that, recovery
from a fault fires every backlogged slot at once and shows up in `metrics.raw` as a
traffic spike that never happened.

Workers still cap concurrency, so under a severe fault the generator falls behind its
target instead of piling on unbounded connections. That is why `GET /stats` reports
`achieved_rps` separately from `target_rps`.

## Checking that traffic was actually running

```bash
curl -s localhost:5002/stats
```

```json
{
  "target_rps": 5.0, "achieved_rps": 4.97, "recent_rps": 4.98, "recent_window_s": 60.0,
  "workers": 4,
  "requests": 17902, "failures": 3, "failure_ratio": 0.0002,
  "journeys": 1627, "orders": 244,
  "by_step": {"home": 1627, "login": 1627, "catalogue": 1627, "...": 0},
  "failures_by_step": {"order": 3},
  "client_errors_by_step": {},
  "last_failure": {"step": "order", "detail": "HTTP 500", "at": 1789300000.0}
}
```

`achieved_rps` is the lifetime average; **`recent_rps` (60s window) is the one to check** —
a generator that ran for an hour and then stalled still has a healthy-looking average.
Either one well below `target_rps` means the testbed is saturated (or a fault is running),
not that the generator is misconfigured.

The fault injector reads `recent_rps` at injection time and stores it on the
scenario as `params.offered_rps_at_inject`, so a run against an idle testbed is visible in
`fault_scenarios` afterwards instead of being indistinguishable from a detector that
missed. `POST /faults` also returns a `warning` when the testbed is nearly idle
(< 1 req/s) or the generator can't be reached — a warning, not a refusal, so a deliberate
idle-baseline run is still possible.

## Status
Verified offline against a stub front-end (Docker Desktop was not running): 4 workers at a
20 req/s target achieved 19.92 req/s over the run, every journey step reached its endpoint,
checkout and the order counter worked, and the catalogue id list was fetched once at
startup rather than per journey. The `/stats` body above is the real shape of the output,
but the traffic numbers in it are illustrative.

Still to verify on a live stack:

- every one of the 7 scraped services reports `latency_p95_ms` (the 4-of-7 gap closes);
- `request_rate` sits near `TARGET_RPS` spread across the chain;
- an injected `bad_deploy_latency` now moves p95 the way `docs/phase8-fault-injection.md`
  measured under manual load;
- whether the 4 workers × 5 req/s default is enough signal without saturating a laptop
  running 25 containers;
- that every journey path matches this `front-end` image's routes. A wrong path answers
  4xx, which isn't counted as a failure (an empty cart legitimately answers 4xx) but is
  counted in `client_errors_by_step` — so a step whose client-error count tracks its
  request count is a wrong path. Worth one look at `curl -s localhost:5002/stats` after
  the first bring-up.
