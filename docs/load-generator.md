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

## Verified on the live stack

Full stack up (25 containers), generator running 15 minutes at the defaults:

```
achieved_rps 4.987 · recent_rps 4.983 · requests 4583 · failures 0 · journeys 499 · orders 52
```

**All 7 scraped services now report latency** — the 4-of-7 gap is closed. Ten minutes of
samples, per service:

| Service | `request_rate` | `latency_p95_ms` samples | p95 max |
| --- | --- | --- | --- |
| front-end | 5.00 | 119 | 86.4 ms |
| catalogue | 2.39 | 119 | 4.9 ms |
| carts | 2.27 | 119 | 33.3 ms |
| user | 1.22 | 119 | 24.2 ms |
| orders | 0.61 | 119 | 213.6 ms |
| payment | 0.28 | 119 | 137.5 ms |
| shipping | 0.06 | 119 | 72.5 ms |

Previously `carts`, `orders`, `shipping` and `front-end` produced no `latency_p95_ms` at
all. `payment` and `shipping` are fed only by checkout, which is why their rates are low —
raise `ORDER_PROBABILITY` to exercise them harder.

**A fault is now observable end to end.** A 90s `bad_deploy_latency` against catalogue took
p95 from 4.8 ms to 160 ms at unchanged request rate, and M2's detector fired on it
(`catalogue/latency_p95_ms value=94.5 baseline=4.83 score=309`, grouped with `front-end`,
tagged to the companion deploy). That is the whole chain issue #6 said was impossible
against an idle testbed. The throttle strength matters, though — see the `cpu_limit` table
in `docs/phase8-fault-injection.md`.

Offered load held flat while the fault ran, which is the open-loop design doing its job.
4 workers × 5 req/s was comfortable on a laptop running the full stack.

## Known, not yet fixed

- **`error_rate` is never ingested** (M2 flagged this too). Nothing returns 5xx, so the
  `request_duration_seconds_count{status_code=~"5.."}` series does not exist, the PromQL
  returns empty rather than zero, and `metrics-bridge` skips the sample. It needs an
  `or vector(0)` in the query — M1's fix, not done here.
- **~27% of checkouts answer 4xx** (`client_errors_by_step: {"order": 19}` against 71
  attempts). Orders still complete — 52 of them — so the path works; the failures are
  most likely a cart/session race in the journey. Not load-affecting, but worth a look.
