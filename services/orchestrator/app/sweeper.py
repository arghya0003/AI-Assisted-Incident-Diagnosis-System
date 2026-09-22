"""Background thread that expires incidents left in AWAITING_APPROVAL past their
`expires_at` (set from APPROVAL_TIMEOUT_SECONDS when M3's analysis completes, and extended by
`request_info`). Without this, an incident nobody ever decides on would sit AWAITING_APPROVAL
forever instead of reaching a terminal state, and the "0 autonomous actions" guarantee
(PLAN.md) already means the safe default here is "nothing happens" -- expiry only marks that
outcome explicitly rather than leaving it ambiguous.
"""

import logging
import threading

from app.settings import Settings
from app.state_machine import Orchestrator

log = logging.getLogger("orchestrator.sweeper")


def run(orchestrator: Orchestrator, settings: Settings, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            expired = orchestrator.sweep_expired()
            if expired:
                log.info("expired %d incident(s): %s", len(expired), expired)
        except Exception:
            log.exception("expiry sweep failed; will retry next interval")
        stop.wait(settings.sweep_interval_seconds)


def start(orchestrator: Orchestrator, settings: Settings) -> tuple[threading.Thread, threading.Event]:
    stop = threading.Event()
    thread = threading.Thread(target=run, args=(orchestrator, settings, stop), daemon=True, name="expiry-sweeper")
    thread.start()
    return thread, stop
