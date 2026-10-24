# Pipeline evidence, 2026-10-05

One `service_crash` on `catalogue`, 150 seconds, captured at every stage. Injected at
08:45:41Z; detected at T+27.6s; awaiting a human at T+65s; approved; fault withdrawn at
T+151s.

These are not regenerable by a command — each one is a moment in a live fault window — so
they are committed rather than gitignored like `results/`.

| File | Shows | Useful for |
| --- | --- | --- |
| `01`, `02` | Sock Shop serving normally | the "before" half of a before/after pair |
| `03`, `04` | **502 Bad Gateway** on the product page | user-visible impact; the fault is real, not simulated |
| `05` | Inject tab, all seven fault classes | the harness, and that six-class coverage is real |
| `06` | Ranked hypotheses with confidence, blamed service, evidence and proposed action | the diagnosis layer |
| `07` | History mid-fault — "fault still running" | the console tracks a fault while it is live |
| `09` | **History after recovery: the whole chain in one row** | the single best image of the system working |
| `10` | Sock Shop serving again | the "after" |
| `08`, `11` | Detection-rate charts | injected vs detected over time, rate per class |
| `12` | **Prometheus `up{job="sock-shop-services"}`** — catalogue flat at 0 for the fault window | independent evidence: a tool nobody here wrote, showing the service was genuinely down |
| `13` | Prometheus targets | scrape health |
| `14` | M4's approval console | the HITL workflow in its own UI |
| `15`, `16` | Audit chain, and `/audit/verify` returning `intact: true` | tamper-evidence, not just logging |

## Two things worth reading off these

**The crash cascaded to six services and the grouper collapsed it into one alert.** Without
that an operator sees six alerts for one incident; `06` shows the single event with its
member list.

**The diagnosis picked `catalogue` out of six anomalous candidates**, including `carts` and
`payment` which were genuinely degraded too — and did so on the deterministic path, with no
language model involved.

## Caveat on `14`

M4's console in that shot shows eight incidents awaiting approval, nearly all `orders-db`.
That was a defect, not normal operation: the detector was alerting on the testbed's own
instrumentation because its image predated the ignore-list change that shipped alongside
the metric. Fixed the same day. Do not use `14` as evidence of normal queue depth.
