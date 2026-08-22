# Phase 0 Decision Record

## Benchmark app: Sock Shop (switched from Train Ticket)
Originally chose Train Ticket for its published fault catalogues (useful for M2's
fault-injection harness later). Switched to **Sock Shop** early in Phase 1 after Train
Ticket turned out to be high-friction for this stage of the project: it's a live,
actively-drifted fork requiring a full Maven/JDK8 build pipeline, hit a real Lombok/JDK
version incompatibility when built with a local (non-containerized) JDK, and its UI
dashboard hard-required a Nacos service-discovery layer + API gateway just to route any
request. None of that blocks the actual goal of this phase (realistic telemetry from a
running microservice system), so Sock Shop was substituted: every service ships as a
pre-built Docker Hub image (`weaveworksdemos/*`), no build step, no service-discovery layer.
Cloned into `testbed/sock-shop/` (upstream `microservices-demo/microservices-demo`, now
archived/deprecated by its maintainers, but the images work standalone in Compose).

Trade-off accepted: Sock Shop has no published fault catalogue the way Train Ticket does,
so M2's fault-injection scenarios (Week 8-9) will need to be authored from scratch rather
than adapted from literature. Revisit only if that becomes a real blocker — worth reopening
with the team if M2 needs the head start more than M1 needs the lower setup friction now.

Note: `testbed/train-ticket/` may still be present on disk as inert leftover — Windows
held a file lock on it during cleanup (likely an editor/terminal with it open). It's
unreferenced by anything and safe to delete manually once unlocked.

## Service subset (14 services, full Sock Shop minus the load generator)
Dropped only `user-sim` (a load-test traffic generator, not part of the causal dependency
graph) from the official reference compose. Kept the rest as-is since these are small,
pre-built, well-tested images — no benefit to trimming further the way Train Ticket's
40-service catalogue needed trimming.

Dependency chain, **verified by extracting each image's actual config/source** (not
guessed — this repo is unmaintained and its own vendored compose file predates some of
what's baked into the current images):

- `edge-router` (Traefik, single entrypoint) → `front-end` (Node.js BFF) — confirmed via
  `traefik.toml` extracted from the image: it proxies everything to `front-end:8079`.
- `front-end` → `catalogue`, `carts`, `orders`, `user` — confirmed via `config.js` /
  `api/endpoints.js` extracted from the front-end image: plain container-hostname REST
  calls (`http://catalogue`, `http://carts/carts`, etc.), no gateway or discovery layer,
  unlike Train Ticket.
- `catalogue` → `catalogue-db` (MySQL)
- `carts` → `carts-db` (Mongo)
- `orders` → `orders-db` (Mongo); confirmed via `application.properties` extracted from
  the orders service jar (`spring.data.mongodb.uri=mongodb://orders-db:27017/data`).
  Checkout also involves `payment`, `shipping`, `user` per the standard Sock Shop
  reference architecture.
- `user` → `user-db` (Mongo)
- `shipping` → `rabbitmq` ← `queue-master` — an async fan-out path, structurally
  different from the REST call chain above (useful later for queue-backlog-style fault
  scenarios M2 might want).

## Docker Desktop
Confirmed installed and working locally (Docker 29.2.1, Compose v5.0.2).

## Repo skeleton
`docker-compose.yml` at repo root pulls all 14 services as pre-built images on a shared
`diagnosis-net` bridge network — `docker compose up` needs no build step at all, satisfying
the project's "clean checkout, no manual steps" definition of done even more directly than
a from-source build would.

## Phase 1 proof it works ✅
Verified end-to-end: `curl http://localhost/catalogue?size=3` through `edge-router` →
`front-end` → `catalogue` → `catalogue-db` returns real product data with HTTP 200.
`orders` logs confirm clean startup (Mongo connection fine, Tomcat up, no restarts).
All 14 containers stayed up with zero restarts.

## Interface contracts
Drafted in `CONTRACTS.md`. Status: **pending team sign-off before Week 3** — this is
unilateral until the other three members confirm the shapes, especially the `severity`
enum on `anomalies.detected` and the `proposed_action` vocabulary. Service names in the
example payloads need updating from the old Train Ticket names to the Sock Shop ones.
