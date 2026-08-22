# Phase 2 Notes: Instrumentation

## Finding: Sock Shop's images are already self-instrumented
Checked before writing any instrumentation code: curled `/metrics` directly on every
running container. Every app service - Go (`catalogue`, `payment`, `user`), Java/Spring
Boot (`carts`, `orders`, `shipping`, `queue-master`), and Node.js (`front-end`) - already
exposes a native Prometheus-format `/metrics` endpoint, with no config changes needed:

- `request_duration_seconds` histogram, labeled by `method`, `route`, `status_code` (and
  `service` on some) - gives latency percentiles (via `histogram_quantile`), request
  volume (via `_count`), and error rate (via filtering `status_code=~"5.."`) all from one
  metric.
- Process-level CPU (`process_cpu_seconds_total` / Go `go_memstats_*`) and memory
  (`process_resident_memory_bytes`, `jvm_memory_bytes_used`, `go_memstats_alloc_bytes`).

This means Phase 2 didn't need Micrometer/OpenTelemetry agents or sidecars the way the
plan originally assumed (that assumption fit a from-source build like Train Ticket, not a
closed-box pre-built image stack) - it needed a Prometheus server to scrape what's already
there.

## What was added
- `prometheus/prometheus.yml` - scrape config targeting all 7 working app services on
  their internal ports (`front-end:8079`, `catalogue:80`, `payment:80`, `user:80`,
  `carts:80`, `orders:80`, `shipping:80`).
- `prometheus` service in `docker-compose.yml`, port 9090 published to host.

## Known gap: queue-master
`queue-master`'s `/metrics` returns legacy Spring Boot Actuator **JSON**
(`{"mem":...,"gauge.response.metrics":...}`), not Prometheus text format, unlike
`carts`/`orders`/`shipping` which are otherwise built the same way. This breaks Prometheus's
scrape parser. Left out of the scrape config rather than faked - a target permanently
shown "down" would be misleading. Not fixed because it's low-value right now (queue-master
just relays RabbitMQ messages to shipping, not a metrics-critical path) - revisit with a
JSON-to-Prometheus exporter sidecar if M2/M3 later need it.

## Known gap: data stores and edge-router
`catalogue-db` (MySQL), `carts-db`/`orders-db`/`user-db` (Mongo), and `rabbitmq` don't
have app-level Prometheus metrics scraped yet - would need `mysqld_exporter` /
`mongodb_exporter` / a RabbitMQ metrics sidecar (this RabbitMQ version, 3.6.8, predates
the built-in Prometheus plugin added in 3.8+). `edge-router` (Traefik) supports Prometheus
metrics via config but isn't enabled yet. None of these are "chosen services" in Phase 2's
literal scope (application-level metrics on the services being monitored), so deferred
rather than blocking. Worth adding later if M2's fault scenarios need DB-connection-pool
or queue-depth signals specifically (e.g. "DB connection-pool saturation" is explicitly
one of the fault types M2's plan calls for).

## Proof it works
Generated real traffic (`curl` loop against `/catalogue` through the edge router), then
queried Prometheus directly:

- All 7 scrape targets report `health: up`.
- `request_duration_seconds_count{instance="catalogue:80",route="catalogue"}` = 21 (real
  request volume, matching the generated traffic).
- `histogram_quantile(0.95, rate(request_duration_seconds_bucket{instance="catalogue:80",
  route="catalogue"}[5m]))` = `0.036` seconds (36ms p95 latency) - a real, non-fabricated
  percentile computed from the histogram.

## Next
Phase 3 (Kafka ingestion) will scrape these same Prometheus endpoints (or query Prometheus
itself) and republish each sample onto the `metrics.raw` Kafka topic in the
`{service, metric, value, timestamp, labels{}}` shape from `CONTRACTS.md`.
