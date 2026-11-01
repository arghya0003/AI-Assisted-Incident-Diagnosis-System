"""
Steady traffic for a dummy site.

A fault on a service with no traffic moves no metric and nothing can detect it,
so every site needs this running. Requests go out on a fixed schedule from a
pool rather than one after another: a client that waited for each answer would
slow down with the fault it is meant to expose, and hide it.
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dummy-traffic")

TARGET_URL = os.environ.get("TARGET_URL", "http://stream-web:8080/work")
RPS = float(os.environ.get("RPS", "5"))
TIMEOUT_S = float(os.environ.get("TIMEOUT_S", "5"))
REPORT_EVERY_S = float(os.environ.get("REPORT_EVERY_S", "30"))


def send(url: str = TARGET_URL) -> bool:
    try:
        return requests.get(url, timeout=TIMEOUT_S).status_code < 500
    except requests.RequestException:
        return False


def run() -> None:
    log.info("sending %.1f req/s to %s", RPS, TARGET_URL)
    interval = 1 / RPS
    sent = failed = 0
    report_at = time.monotonic() + REPORT_EVERY_S
    next_at = time.monotonic()
    pending = []

    with ThreadPoolExecutor(max_workers=int(RPS * TIMEOUT_S) + 1) as pool:
        while True:
            pending.append(pool.submit(send))
            sent += 1
            next_at += interval
            time.sleep(max(0.0, next_at - time.monotonic()))

            still_running = []
            for future in pending:
                if not future.done():
                    still_running.append(future)
                elif not future.result():
                    failed += 1
            pending = still_running

            if time.monotonic() >= report_at:
                log.info("sent %d, failed %d", sent, failed)
                report_at += REPORT_EVERY_S


if __name__ == "__main__":
    run()
