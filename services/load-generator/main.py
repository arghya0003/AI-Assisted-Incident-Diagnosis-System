"""
Standing load generator (fix for issue #6 - "no standing traffic").

Without continuous traffic the testbed is idle: request_rate sits at ~0.2
req/s, only 3 of 7 services produce a latency histogram at all, and an
injected fault moves no metric - so nothing downstream can detect or
diagnose it. The original Sock Shop `user-sim` was dropped in Phase 0
because it isn't part of the causal dependency graph; that was right for
the graph and wrong for the data plane.

This replaces it with a generator we control: a fixed, known offered load
through `edge-router`, exercising the whole call graph rather than one
endpoint -

    /              front-end
    /login         front-end -> user
    /catalogue     front-end -> catalogue -> catalogue-db
    /cart          front-end -> carts     -> carts-db
    /orders        front-end -> orders    -> payment, shipping, user, carts
                                shipping  -> rabbitmq -> queue-master

Pacing is open-loop: a shared rate limiter hands out one request slot
every 1/TARGET_RPS seconds regardless of how slow the testbed is. A
closed-loop generator (fixed think time after each response) would quietly
*reduce* offered load exactly when a fault slows the system down, which is
the moment the load matters most. Workers cap concurrency, so under a
severe fault the generator falls behind its target rather than piling on
unbounded - and `GET /stats` reports the rate actually achieved, so an
evaluation run can check what load was really offered instead of assuming.

`GET /stats` on port 5002 is the answer to "was traffic actually running
during that fault?" - see docs/load-generator.md.
"""

import os
import random
import logging
import threading
import time
from collections import Counter, deque

import requests
from flask import Flask, jsonify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("load-generator")

TARGET_URL = os.environ.get("TARGET_URL", "http://edge-router").rstrip("/")
WORKERS = int(os.environ.get("WORKERS", "4"))
TARGET_RPS = float(os.environ.get("TARGET_RPS", "5"))
# Checkout is the expensive journey (orders -> payment/shipping/user/carts,
# plus a row in orders-db that nothing cleans up), so only a fraction of
# journeys check out. 0 disables checkout entirely - payment and shipping
# then get no traffic at all.
ORDER_PROBABILITY = float(os.environ.get("ORDER_PROBABILITY", "0.15"))
REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "10"))
CATALOGUE_REFRESH_SECONDS = float(os.environ.get("CATALOGUE_REFRESH_SECONDS", "300"))
EMPTY_CATALOGUE_RETRY_SECONDS = float(os.environ.get("EMPTY_CATALOGUE_RETRY_SECONDS", "15"))
# Window for `recent_rps`. A lifetime average can't tell "traffic is flowing"
# from "traffic flowed for an hour and then stalled", which is exactly the
# question the fault injector asks this service.
RECENT_WINDOW_SECONDS = float(os.environ.get("RECENT_WINDOW_SECONDS", "60"))
# Seeded Sock Shop customer - the user-db image ships it with an address
# and a card, which POST /orders needs.
SHOP_USER = os.environ.get("SHOP_USER", "user")
SHOP_PASSWORD = os.environ.get("SHOP_PASSWORD", "password")
PORT = int(os.environ.get("PORT", "5002"))

app = Flask(__name__)


class RateLimiter:
    """Hands out one slot every 1/rps seconds, shared across all workers."""

    def __init__(self, rps: float):
        self._interval = 1.0 / rps if rps > 0 else 0.0
        self._lock = threading.Lock()
        self._next_slot = time.monotonic()

    def acquire(self) -> None:
        if self._interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            # Never bank credit for time spent stalled behind a slow testbed:
            # otherwise recovery from a fault fires off every backlogged slot
            # at once and shows up in the metrics as a fake traffic spike.
            if self._next_slot < now:
                self._next_slot = now
            wait = self._next_slot - now
            self._next_slot += self._interval
        if wait > 0:
            time.sleep(wait)


limiter = RateLimiter(TARGET_RPS)

_stats_lock = threading.Lock()
_stats = {
    "started_at": time.time(),
    "requests": 0,
    "failures": 0,
    "journeys": 0,
    "orders": 0,
    "by_step": Counter(),
    "failures_by_step": Counter(),
    # 4xx isn't a failure (an empty cart answers 4xx), but a step that is 4xx
    # *every* time is a wrong path against this front-end image, and that
    # should be visible rather than silent.
    "client_errors_by_step": Counter(),
    "last_failure": None,
}
_recent_requests: deque[float] = deque()

_catalogue_lock = threading.Lock()
_catalogue_ids: list[str] = []
_catalogue_attempted_at = float("-inf")


def record(step: str, ok: bool, detail: str | None = None, client_error: bool = False) -> None:
    now = time.time()
    with _stats_lock:
        _recent_requests.append(now)
        cutoff = now - RECENT_WINDOW_SECONDS
        while _recent_requests and _recent_requests[0] < cutoff:
            _recent_requests.popleft()
        _stats["requests"] += 1
        _stats["by_step"][step] += 1
        if client_error:
            _stats["client_errors_by_step"][step] += 1
        if not ok:
            _stats["failures"] += 1
            _stats["failures_by_step"][step] += 1
            _stats["last_failure"] = {"step": step, "detail": detail, "at": time.time()}


def call(session: requests.Session, step: str, method: str, path: str, **kwargs):
    """One paced request. Never raises - a fault is supposed to break these."""
    limiter.acquire()
    try:
        resp = session.request(method, f"{TARGET_URL}{path}", timeout=REQUEST_TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        record(step, False, f"{type(exc).__name__}: {exc}")
        return None
    # 4xx is the app answering (empty cart, no orders yet); 5xx and timeouts
    # are the failures a fault is expected to produce.
    record(step, resp.status_code < 500, f"HTTP {resp.status_code}",
           client_error=400 <= resp.status_code < 500)
    return resp


def refresh_catalogue(force: bool = False) -> list[str]:
    """Item ids, read from the running catalogue rather than hardcoded.

    This one request is outside the rate limiter, so it is deliberately
    rate-limited by attempt time instead - including after a failure. Without
    that, a fault against `catalogue` would make every journey retry the
    fetch, adding unmetered load and blocking workers on the timeout at
    exactly the moment offered load is supposed to stay flat.
    """
    global _catalogue_attempted_at, _catalogue_ids
    now = time.monotonic()
    with _catalogue_lock:
        # Retry sooner while the list is still empty - nothing else in the
        # journey past `/catalogue` can run without ids.
        interval = CATALOGUE_REFRESH_SECONDS if _catalogue_ids else EMPTY_CATALOGUE_RETRY_SECONDS
        if not force and now - _catalogue_attempted_at < interval:
            return _catalogue_ids
        _catalogue_attempted_at = now

    try:
        resp = requests.get(f"{TARGET_URL}/catalogue?size=100", timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        ids = [item["id"] for item in resp.json() if item.get("id")]
    except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
        log.warning("could not refresh catalogue ids: %s", exc)
        return _catalogue_ids

    if ids:
        with _catalogue_lock:
            _catalogue_ids = ids
        log.info("catalogue has %d items", len(ids))
    return _catalogue_ids


def journey(session: requests.Session) -> None:
    """One browse-and-maybe-buy pass over the dependency chain."""
    call(session, "home", "GET", "/")
    call(session, "login", "GET", "/login", auth=(SHOP_USER, SHOP_PASSWORD))
    call(session, "catalogue", "GET", "/catalogue?size=5&page=1")
    call(session, "catalogue_size", "GET", "/catalogue/size")

    item_ids = refresh_catalogue()
    if item_ids:
        item_id = random.choice(item_ids)
        call(session, "detail", "GET", f"/catalogue/{item_id}")
        call(session, "cart_clear", "DELETE", "/cart")
        call(session, "cart_add", "POST", "/cart", json={"id": item_id, "quantity": 1})
        call(session, "cart_view", "GET", "/cart")
        if random.random() < ORDER_PROBABILITY:
            resp = call(session, "order", "POST", "/orders")
            if resp is not None and resp.status_code < 400:
                with _stats_lock:
                    _stats["orders"] += 1

    call(session, "order_history", "GET", "/orders")

    with _stats_lock:
        _stats["journeys"] += 1


def worker(index: int) -> None:
    session = requests.Session()
    session.headers["User-Agent"] = f"incident-diagnosis-load-generator/{index}"
    while True:
        try:
            journey(session)
        except Exception:
            # A worker dying would silently halve offered load, and the
            # metrics would read as a traffic drop rather than as a bug here.
            log.exception("worker %d: journey failed unexpectedly", index)
            time.sleep(1)


def wait_for_testbed() -> None:
    while True:
        try:
            requests.get(f"{TARGET_URL}/", timeout=REQUEST_TIMEOUT)
            log.info("testbed reachable at %s", TARGET_URL)
            return
        except requests.RequestException as exc:
            log.warning("testbed not reachable yet at %s (%s), retrying in 3s", TARGET_URL, exc)
            time.sleep(3)


@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.get("/stats")
def get_stats():
    """What load was actually offered - not what was configured."""
    now = time.time()
    with _stats_lock:
        elapsed = max(now - _stats["started_at"], 1e-9)
        requests_total = _stats["requests"]
        failures = _stats["failures"]
        cutoff = now - RECENT_WINDOW_SECONDS
        while _recent_requests and _recent_requests[0] < cutoff:
            _recent_requests.popleft()
        recent_window = min(elapsed, RECENT_WINDOW_SECONDS)
        return jsonify({
            "target_url": TARGET_URL,
            "workers": WORKERS,
            "target_rps": TARGET_RPS,
            # Lifetime average; `recent_rps` is what "is traffic flowing right
            # now" should be judged on.
            "achieved_rps": round(requests_total / elapsed, 3),
            "recent_rps": round(len(_recent_requests) / recent_window, 3),
            "recent_window_s": RECENT_WINDOW_SECONDS,
            "order_probability": ORDER_PROBABILITY,
            "uptime_s": round(elapsed, 1),
            "requests": requests_total,
            "failures": failures,
            "failure_ratio": round(failures / requests_total, 4) if requests_total else 0.0,
            "journeys": _stats["journeys"],
            "orders": _stats["orders"],
            "by_step": dict(_stats["by_step"]),
            "failures_by_step": dict(_stats["failures_by_step"]),
            "client_errors_by_step": dict(_stats["client_errors_by_step"]),
            "last_failure": _stats["last_failure"],
        })


def main() -> None:
    wait_for_testbed()
    refresh_catalogue(force=True)
    log.info("offering %.2f req/s across %d workers against %s", TARGET_RPS, WORKERS, TARGET_URL)
    for index in range(WORKERS):
        threading.Thread(target=worker, args=(index,), daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
