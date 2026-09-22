"""The immutable audit log (PLAN.md safety architecture, item c): who approved what, when, on
what evidence, with which model version.

Two layers of immutability, deliberately different in strength:

  1. Postgres-level: `008_incidents.sql` adds a trigger on `audit_log` that raises on UPDATE
     or DELETE, so an application bug (or a compromised orchestrator process) cannot rewrite
     history through normal SQL.
  2. Hash chain: every row's `hash` commits to its own content plus the previous row's hash
     (`hash_entry` below), so an edit made a different way -- a manual UPDATE by a superuser
     bypassing the trigger, a restored backup with one row swapped -- changes that row's hash
     and breaks every hash after it. `verify_chain` in db.py walks the table and reports the
     first break.

This is tamper-evidence, not tamper-prevention: a superuser with table-owner rights can still
edit Postgres directly (e.g. `ALTER TABLE ... DISABLE TRIGGER`) and recompute the chain from
that point forward. Defending against that needs an external append-only log (e.g. shipping
rows to write-once storage), which is out of scope for a capstone -- documented here rather
than silently assumed away, per PLAN.md's "their limitations stated explicitly".
"""

import hashlib
import json

GENESIS_HASH = "0" * 64


def _canonical(payload: dict) -> str:
    # sort_keys + separators so the same logical content always hashes the same way.
    return json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))


def hash_entry(prev_hash: str, audit_id: int, incident_id: str | None, event_type: str, actor: str, detail: dict) -> str:
    body = _canonical(
        {
            "prev_hash": prev_hash,
            "audit_id": audit_id,
            "incident_id": incident_id,
            "event_type": event_type,
            "actor": actor,
            "detail": detail,
        }
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
