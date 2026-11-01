"""
One small service, configured by environment, that plays every part in a dummy
test site.

These sites exist so the detector and the diagnosis layer can be tried on
something they were not tuned on. Nothing here has to be realistic except the
signals: the same `request_duration_seconds` histogram and process metrics the
Sock Shop services publish, so metrics-bridge reads them with no changes.

Two things make a fault behave like one instead of being painted on:

- Downstream calls are real and synchronous, so a slow or dead service slows or
  fails everything upstream of it. The cascade the grouper and ranker have to
  deal with arises on its own.
- Each request burns real CPU. A cgroup throttle only bites below what a service
  actually uses; catalogue idled at 0.17% of a core and the first CPU fault
  changed nothing, so a service that only slept would repeat that.
"""

from __future__ import annotations

import logging
import os
import random
import time
from dataclasses import dataclass, field

import requests
from flask import Flask, Response, jsonify, send_from_directory
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Histogram, generate_latest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dummy-service")

# Wide enough to resolve a 5 ms baseline and a timeout cascade with one
# histogram, since the bridge derives p50/p95/p99 from these buckets.
LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)


@dataclass(frozen=True)
class Config:
    name: str = "dummy"
    downstream: tuple[str, ...] = ()
    work_ms: float = 2.0
    latency_ms: float = 5.0
    jitter_ms: float = 2.0
    error_rate: float = 0.0
    timeout_s: float = 2.0

    @classmethod
    def from_env(cls, env=None) -> "Config":
        env = os.environ if env is None else env
        return cls(
            name=env.get("SERVICE_NAME", "dummy"),
            downstream=parse_downstream(env.get("DOWNSTREAM", "")),
            work_ms=float(env.get("WORK_MS", "2")),
            latency_ms=float(env.get("LATENCY_MS", "5")),
            jitter_ms=float(env.get("JITTER_MS", "2")),
            error_rate=float(env.get("ERROR_RATE", "0")),
            timeout_s=float(env.get("DOWNSTREAM_TIMEOUT_S", "2")),
        )


def parse_downstream(value: str) -> tuple[str, ...]:
    """`stream-storage:8080, x:8080` -> base URLs, in call order."""
    hosts = [h.strip() for h in value.split(",") if h.strip()]
    return tuple(h if h.startswith("http") else f"http://{h}" for h in hosts)


def burn_cpu(milliseconds: float) -> None:
    """Spin rather than sleep, so a CPU quota has something to take away."""
    deadline = time.perf_counter() + milliseconds / 1000
    while time.perf_counter() < deadline:
        sum(i * i for i in range(200))


def create_app(config: Config, call=requests.get, registry=REGISTRY, rng=random.random) -> Flask:
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    # status_code is the label metrics-bridge filters on for error_rate.
    latency = Histogram(
        "request_duration_seconds", "Time to serve /work",
        ["route", "status_code"], registry=registry, buckets=LATENCY_BUCKETS,
    )

    def downstream_failure() -> tuple[int, str] | None:
        for url in config.downstream:
            try:
                response = call(f"{url}/work", timeout=config.timeout_s)
            except requests.Timeout:
                return 504, f"timed out calling {url}"
            except requests.RequestException:
                return 502, f"could not reach {url}"
            if response.status_code >= 500:
                return 502, f"{url} answered {response.status_code}"
        return None

    @app.get("/work")
    def work():
        started = time.perf_counter()
        burn_cpu(config.work_ms)
        time.sleep(max(0.0, config.latency_ms + rng() * config.jitter_ms) / 1000)

        failure = downstream_failure()
        if failure is None and rng() < config.error_rate:
            failure = (500, "configured error rate")

        status, detail = failure or (200, "ok")
        elapsed = time.perf_counter() - started
        latency.labels(route="/work", status_code=str(status)).observe(elapsed)
        return jsonify({"service": config.name, "detail": detail,
                        "elapsed_ms": round(elapsed * 1000, 1)}), status

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok", "service": config.name})

    @app.get("/metrics")
    def metrics():
        return Response(generate_latest(registry), mimetype=CONTENT_TYPE_LATEST)

    @app.get("/")
    def index():
        # Only the site's entry service has a page; the others just answer /work.
        return send_from_directory("static", "player.html")

    return app


if __name__ == "__main__":
    settings = Config.from_env()
    log.info("%s calling %s", settings.name, list(settings.downstream) or "nothing")
    create_app(settings).run(host="0.0.0.0", port=8080, threaded=True)
