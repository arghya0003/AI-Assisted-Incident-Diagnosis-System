"""
Bounded order history (fix for issue #33).

Checkout writes a `customerOrder` document to orders-db, and every journey
then reads the whole history back with `GET /orders`. All of those orders
belong to the one seeded customer, so the history response grows with every
checkout. Left alone it reached 2,152 documents in about a day: `orders` p95
went from ~45 ms to 2.3 s and the service eventually stopped answering. Every
evaluation run after that measured a sicker testbed than the one before.

Two controls, both owned here because this service is what creates the data:

  prune   Keep at most `max_documents` orders, deleting the oldest. Runs on an
          interval, so the history - and with it `orders` latency - stays in
          a fixed band however long the stack has been up.
  reset   Delete every order. An evaluation run calls this before warm-up
          (POST /reset) so every run starts from the same baseline instead of
          one that depends on how long the generator has been running.

The collection is passed in rather than created here, so the logic is
testable without a live MongoDB.
"""

from __future__ import annotations

import logging

log = logging.getLogger("load-generator")


def count(collection) -> int:
    return collection.count_documents({})


def prune(collection, max_documents: int) -> int:
    """Delete the oldest orders above `max_documents`; return how many went.

    Oldest by `_id`: Spring Data stores the order id as an ObjectId, whose
    leading bytes are the creation time, so `_id` order is insertion order.
    """
    if max_documents <= 0:
        return 0
    excess = count(collection) - max_documents
    if excess <= 0:
        return 0
    oldest = [doc["_id"] for doc in collection.find({}, {"_id": 1}).sort("_id", 1).limit(excess)]
    if not oldest:
        return 0
    deleted = collection.delete_many({"_id": {"$in": oldest}}).deleted_count
    log.info("pruned %d order(s) from orders-db (ceiling %d)", deleted, max_documents)
    return deleted


def reset(collection) -> int:
    """Delete every order; return how many there were."""
    deleted = collection.delete_many({}).deleted_count
    log.info("reset orders-db: removed %d order(s)", deleted)
    return deleted


def prometheus_text(documents: int | None, max_documents: int, pruned_total: int) -> str:
    """The order history as Prometheus text, for `GET /metrics`.

    `/stats` already reported the document count, but only as a reading of
    right now: nothing stored it, so `orders` latency could not be lined up
    against it after the fact (issue #33, reopened). Scraped by Prometheus,
    it reaches TimescaleDB through metrics-bridge like every other metric.

    The pruned counter is there because the open question is churn, not
    size: the count sits at the ceiling while the pruner keeps deleting, so
    the count alone cannot tell the two apart. It restarts at 0 on `/reset`,
    which Prometheus's rate() reads as an ordinary counter reset.

    `documents` is None when orders-db is unreachable, and the sample is then
    left out rather than reported as 0 - an empty history and an unreachable
    database are different states, and 0 would look like the first one.
    """
    lines = [
        "# HELP orders_db_max_documents Ceiling the pruner holds the order history under; 0 means pruning is off.",
        "# TYPE orders_db_max_documents gauge",
        f"orders_db_max_documents {max_documents}",
        "# HELP orders_db_pruned_total Orders the pruner has deleted since start or the last reset.",
        "# TYPE orders_db_pruned_total counter",
        f"orders_db_pruned_total {pruned_total}",
    ]
    if documents is not None:
        lines += [
            "# HELP orders_db_documents Orders in orders-db, all of which GET /orders returns.",
            "# TYPE orders_db_documents gauge",
            f"orders_db_documents {documents}",
        ]
    return "\n".join(lines) + "\n"
