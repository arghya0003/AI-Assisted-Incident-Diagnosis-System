"""The stubbed remediation executor (PLAN.md safety architecture, item b): logs intent only,
and is architecturally incapable of touching the running system.

This is a *hard* gate, not a policy one -- the distinction PLAN.md asks for ("must be real and
not a paragraph in the report"). Three separate, independent reasons this process cannot act on
the testbed, any one of which would be enough on its own:

  1. No capability to act. This module contains no Docker client, no Kubernetes client, no SSH,
     no HTTP client aimed at fault-injector/deploy-emitter/the testbed's own services -- there
     is nothing here *to* call. Compare `services/fault-injector/`, which mounts
     `/var/run/docker.sock` specifically so it *can* act; the orchestrator container in
     docker-compose.yml mounts nothing and is granted no credentials to any control plane.
  2. No target. `execute()` below takes a `ProposedAction` (verb + optional target service) and
     does exactly one thing with it: build a structured log line and an audit_log row. There is
     no code path from that action to a network call, a subprocess, or a file write outside the
     audit table.
  3. It runs after the state machine already enforces human approval. `execute()` is only ever
     called from the orchestrator on an incident that has reached APPROVED (main.py), which
     requires a human decision through `POST /incidents/{id}/approve` -- there is no way to
     reach this function from an incident still in AWAITING_APPROVAL.

Actually executing an approved action (calling fault-injector's rollback, restarting a
container) is listed as future work in PLAN.md's risk register ("Scope creep into
auto-remediation ... The executor is architecturally stubbed by design"), not a stretch goal.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from app.models import Hypothesis, action_verb, parse_action

log = logging.getLogger("orchestrator.executor")


@dataclass(frozen=True)
class ExecutionIntent:
    incident_id: str
    verb: str
    target: str | None
    blast_radius: str
    approved_by: str
    hypothesis_rank: int
    logged_at: str


def build_intent(incident_id: str, hypothesis: Hypothesis, approved_by: str) -> ExecutionIntent:
    verb, target = parse_action(hypothesis.proposed_action)
    return ExecutionIntent(
        incident_id=incident_id,
        verb=verb,
        target=target,
        blast_radius=hypothesis.blast_radius,
        approved_by=approved_by,
        hypothesis_rank=hypothesis.rank,
        logged_at=datetime.now(timezone.utc).isoformat(),
    )


def execute(intent: ExecutionIntent) -> dict:
    """"Execute" an approved remediation. This function's entire body is a log line: no
    network call, no subprocess, no filesystem write outside what the caller does with the
    returned dict (an audit_log row). See the module docstring for why that is by design."""
    log.warning(
        "STUBBED EXECUTOR: intent logged only, no action taken -- incident=%s verb=%s target=%s "
        "blast_radius=%r approved_by=%s hypothesis_rank=%d",
        intent.incident_id, intent.verb, intent.target, intent.blast_radius,
        intent.approved_by, intent.hypothesis_rank,
    )
    return {
        "verb": intent.verb,
        "target": intent.target,
        "blast_radius": intent.blast_radius,
        "approved_by": intent.approved_by,
        "hypothesis_rank": intent.hypothesis_rank,
        "logged_at": intent.logged_at,
        "executed": False,
        "note": "stubbed executor: intent logged only, never acts on the running system",
    }


__all__ = ["ExecutionIntent", "build_intent", "execute", "action_verb"]
