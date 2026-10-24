"""Past outcomes in this system, from the anomaly dataset, for the LLM prompt.

The corpus (app/corpus.py) is other companies' postmortems. This is the opposite end: anomalies
this testbed actually raised, and what they turned out to be. The eval-dashboard writes them to
`results/anomaly_dataset.csv` (services/eval-dashboard/recent.py); this module reads that file.

Only a row whose cause was confirmed is used, and it is labelled by how:

- injected: a labelled fault explained it and has since ended. The fault injector recorded the
  root-cause service, so this is ground truth.
- approved: a human approved a hypothesis; its service is the root cause they agreed to.
- rejected: a human rejected the diagnosis. A negative label: "it was not X".

Everything else - anomalies nobody injected or decided on - is skipped. Its only label would be the
diagnosis this service itself produced, and feeding a model its own unchecked answers as history
would reinforce whatever it already gets wrong.

**Leakage.** The dataset holds the answers, so selection is strict about time. A past outcome is
usable for an anomaly only if its label was known *before that anomaly began*: an injected fault
must have ended, a decision must have been made. That rules out the obvious leak - the first anomaly
of a fault that is still running sits in the file labelled with the injected service, and a later
anomaly from the same fault would otherwise be handed its own answer - and anything later in time.
Without it, live accuracy would measure the label, not the reasoning.

History feeds the LLM only, never the deterministic scorer: the scorer is the measured baseline,
and leaving it untouched keeps "does history help the LLM" a question the ablation can answer.
"""

import csv
import logging
import os
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from app.models import AnomalyEvent

log = logging.getLogger("diagnosis-service.history")

Label = Literal["injected", "approved", "rejected"]

# Similarity is structural, not embedded: the services and metrics a past anomaly shares with this
# one. Services dominate because the label is a service; metrics separate a latency fault from a
# crash on the same service; the detector is a tie-breaker between otherwise equal matches.
SERVICE_WEIGHT = 0.6
METRIC_WEIGHT = 0.3
DETECTOR_WEIGHT = 0.1

# Fault classes that break a dependency rather than the service they are recorded against. Mirrors
# the injector's `dependency` parameter (services/fault-injector/params.py); see injected_root_cause().
DEPENDENCY_FAULT_TYPES = ("dependency_timeout", "config_error")


@dataclass(frozen=True)
class PastOutcome:
    anomaly_id: str
    t_onset: datetime
    services: tuple[str, ...]
    metrics: tuple[str, ...]
    detector: str
    label: Label
    known_at: datetime  # when the label became known: fault ended, or decision made
    root_cause: str | None  # the confirmed service; None for a rejection
    fault_type: str = ""
    diagnosed: str = ""  # what this service blamed at the time
    diagnosis_correct: str = ""  # "yes" | "no" | "" (no ground truth)
    fix: str = ""  # the approved action, if any
    reason: str = ""  # a rejection's stated reason
    group: str = ""  # one fault raises several anomalies; they share a group
    similarity: float = 0.0  # filled by select()


def _time(value: str) -> datetime | None:
    """ISO time, as UTC when the file gives no offset (the dashboard always does)."""
    try:
        moment = datetime.fromisoformat(value) if value else None
    except ValueError:
        return None
    return moment.replace(tzinfo=timezone.utc) if moment and moment.tzinfo is None else moment


def _list(value: str) -> tuple[str, ...]:
    return tuple(part for part in (value or "").split(";") if part)


def injected_root_cause(row: dict[str, str]) -> str | None:
    """The service an injected fault actually broke, or None when this row cannot say.

    Not `ground_truth_service`. For `dependency_timeout` and `config_error` the injector breaks a
    dependency and records the service that visibly degrades: pausing `catalogue-db` is a `catalogue`
    fault in that column. Taking it as the root cause would tell the model "root cause catalogue" and
    make the past case citable under the symptom. The dashboard resolves the split into
    `root_cause_service` (services/eval-dashboard/recent.py, `root_cause_service()`).

    Rows written before that column existed are still on disk - the dataset keeps rows the database
    lost - and for the dependency classes they carry no record of what broke. Those are skipped
    rather than guessed: guessing `ground_truth_service` is the exact mislabel this exists to stop.
    For every other class the two columns agree, so an old row is still usable.
    """
    if "root_cause_service" in row:
        return row["root_cause_service"] or None
    if row.get("fault_type") in DEPENDENCY_FAULT_TYPES:
        return None
    return row.get("ground_truth_service") or None


def parse_row(row: dict[str, str]) -> PastOutcome | None:
    """One dataset row -> a labelled outcome, or None when nothing confirmed its cause."""
    onset = _time(row.get("t_onset", "")) or _time(row.get("t_detected", ""))
    if not row.get("anomaly_id") or onset is None:
        return None
    # An old row's verdict on a dependency fault was scored against the symptom, so it may be
    # inverted; drop the verdict rather than show the model a right answer marked "(wrong)".
    stale_verdict = "root_cause_service" not in row and row.get("fault_type") in DEPENDENCY_FAULT_TYPES
    common = {
        "anomaly_id": row["anomaly_id"],
        "t_onset": onset,
        "services": _list(row.get("services", "")),
        "metrics": _list(row.get("metrics", "")),
        "detector": row.get("detector", ""),
        "fault_type": row.get("fault_type", ""),
        "diagnosed": row.get("diagnosed_service", ""),
        "diagnosis_correct": "" if stale_verdict else row.get("diagnosis_correct", ""),
        "fix": row.get("fix", ""),
        "group": row.get("scenario_id") or row["anomaly_id"],
    }

    resolved_at = _time(row.get("resolved_at", ""))
    root = injected_root_cause(row) if row.get("cause_source") == "injected_fault" else None
    if root and resolved_at:
        return PastOutcome(label="injected", known_at=resolved_at, root_cause=root, **common)

    decided_at = _time(row.get("decided_at", ""))
    if decided_at is None:
        return None
    if row.get("decision") == "approved" and row.get("fix_service"):
        return PastOutcome(label="approved", known_at=decided_at, root_cause=row["fix_service"], **common)
    if row.get("decision") == "rejected" and row.get("diagnosed_service"):
        return PastOutcome(
            label="rejected", known_at=decided_at, root_cause=None, reason=row.get("decision_reason", ""), **common
        )
    return None


def _jaccard(a: tuple[str, ...], b: list[str]) -> float:
    left, right = set(a), set(b)
    return len(left & right) / len(left | right) if left | right else 0.0


def select(outcomes: list[PastOutcome], anomaly: AnomalyEvent, top_k: int) -> list[PastOutcome]:
    """The `top_k` past outcomes most like `anomaly` whose labels were known before it began."""
    if top_k <= 0:
        return []
    scored = []
    for past in outcomes:
        if past.anomaly_id == anomaly.anomaly_id or past.known_at >= anomaly.t_onset:
            continue
        if not set(past.services) & set(anomaly.services):
            continue
        similarity = (
            SERVICE_WEIGHT * _jaccard(past.services, anomaly.services)
            + METRIC_WEIGHT * _jaccard(past.metrics, anomaly.metrics)
            + DETECTOR_WEIGHT * (1.0 if past.detector and past.detector == anomaly.detector else 0.0)
        )
        scored.append(replace(past, similarity=round(similarity, 3)))

    # Most similar first, then most recent. One per group: a single fault raising five anomalies is
    # one past case, and five copies of it would crowd out every other.
    scored.sort(key=lambda p: (-p.similarity, -p.t_onset.timestamp(), p.anomaly_id))
    chosen, groups = [], set()
    for past in scored:
        if past.group in groups:
            continue
        groups.add(past.group)
        chosen.append(past)
        if len(chosen) == top_k:
            break
    return chosen


class HistoryStore:
    """The dataset file, re-read only when it changes.

    The dashboard rewrites the file every 30 s by renaming a new one into place, so a changed
    modification time is a complete new file, never a half-written one. A missing or unreadable file
    is not an error for /analyze: the prompt simply has no past outcomes, and /health says why.
    """

    def __init__(self, path: str | Path | None):
        self._path = Path(path) if path else None
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._rows = 0
        self._outcomes: list[PastOutcome] = []
        self._error: str | None = None

    def outcomes(self) -> list[PastOutcome]:
        if self._path is None:
            return []
        with self._lock:
            try:
                mtime = os.stat(self._path).st_mtime
            except OSError as exc:
                self._error, self._outcomes, self._rows, self._mtime = f"{exc.strerror}: {self._path}", [], 0, None
                return []
            if mtime != self._mtime:
                try:
                    with self._path.open(newline="", encoding="utf-8") as f:
                        rows = list(csv.DictReader(f))
                except (OSError, csv.Error, UnicodeDecodeError) as exc:
                    log.warning("could not read the anomaly dataset %s: %s", self._path, exc)
                    self._error = str(exc)
                    return self._outcomes  # keep the last good read
                parsed = [parse_row(row) for row in rows]
                self._outcomes = [p for p in parsed if p is not None]
                self._rows, self._mtime, self._error = len(rows), mtime, None
                log.info("anomaly dataset: %d rows, %d with a confirmed cause", self._rows, len(self._outcomes))
            return self._outcomes

    def select(self, anomaly: AnomalyEvent, top_k: int) -> list[PastOutcome]:
        return select(self.outcomes(), anomaly, top_k)

    def status(self) -> dict[str, object]:
        if self._path is None:
            return {"status": "disabled"}
        usable = len(self.outcomes())
        if self._error:
            return {"status": "unavailable", "path": str(self._path), "error": self._error}
        return {"status": "ready" if usable else "no_confirmed_outcomes", "path": str(self._path),
                "rows": self._rows, "confirmed": usable}
