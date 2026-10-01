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
