# orchestrator-ui (Member 4)

The human-in-the-loop approval console: anomaly evidence, ranked root-cause hypotheses with
their evidence chain and blast radius, and explicit Approve / Reject / Request-more-info
actions. Talks to `services/orchestrator/`'s REST API and its `/ws` live feed.

Design notes: [docs/phase-m4-orchestration.md](../../docs/phase-m4-orchestration.md).

## Stack
Vite + React 19 + TypeScript + Tailwind v4, no router (two tabs: Incidents, Audit log).

## Run

**In Docker** (part of the root `docker-compose.yml`, port 3000):
```bash
docker compose up -d orchestrator-ui
```
`Dockerfile` is a two-stage build: `npm run build` in a `node:22-slim` stage, served by
nginx. `nginx.conf` proxies `/api/*` and `/ws` to the `orchestrator` container, so the app
only ever talks to its own origin — no CORS handling needed.

**Locally**, against an orchestrator already running (in Docker or on `localhost:8090`):
```bash
npm install
npm run dev
```
`vite.config.ts`'s dev-server proxy maps `/api` and `/ws` to `localhost:8090`, mirroring
`nginx.conf`'s production routing.

## Checks
```bash
npm run build   # tsc -b && vite build
npm run lint     # oxlint
```
