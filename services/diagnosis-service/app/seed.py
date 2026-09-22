"""Precomputed corpus embeddings, so a clean checkout has a working corpus without Ollama.

Issue #19: `corpus/ingest.py` embeds the incident write-ups with nomic-embed-text, which needs
Ollama on the host. On a machine without it - M4's, for one - a fresh TimescaleDB volume leaves
`incidents` empty, and the service still answers: retrieval returns nothing, incident similarity
contributes 0 to every score, and an evaluation silently measures a system with the
retrieval-augmented half switched off.

The fix is to commit the vectors (`corpus/embeddings.jsonl`, regenerated with
`python corpus/ingest.py --write-embeddings`) and load them when the table is empty. Embedding is
deterministic for a fixed model and text, so a stored vector is exactly what ingestion would have
produced - it is a cache of a pure function, not a second source of truth.

Each line records the sha256 of the exact text that was embedded. If a write-up is edited without
regenerating, the hash no longer matches and the stale vector is refused rather than used, because
a silently wrong vector would be worse than the empty table this replaces.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from app.corpus import IncidentRecord, load_corpus
from app.retrieval import EMBEDDING_DIMENSIONS, document_text

log = logging.getLogger("diagnosis-service.seed")

EMBEDDINGS_PATH = Path(__file__).resolve().parent.parent / "corpus" / "embeddings.jsonl"
# Cosine similarity does not need more than this, and it keeps the committed file near 500 KB
# rather than a megabyte of digits nobody reads.
VECTOR_PRECISION = 6


class StaleEmbeddings(RuntimeError):
    """A committed vector no longer matches the text it was made from."""


@dataclass(frozen=True)
class StoredEmbedding:
    incident_id: str
    model: str
    text_sha256: str
    vector: list[float]


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_embeddings(records: list[IncidentRecord], vectors: list[list[float]], model: str, path: Path = EMBEDDINGS_PATH) -> int:
    """Write one line per incident, ordered by id so the committed file has a stable diff."""
    lines = [
        json.dumps(
            {
                "incident_id": record.incident_id,
                "model": model,
                "text_sha256": text_digest(document_text(record)),
                "vector": [round(value, VECTOR_PRECISION) for value in vector],
            },
            sort_keys=True,
        )
        for record, vector in sorted(zip(records, vectors), key=lambda pair: pair[0].incident_id)
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


def load_embeddings(path: Path = EMBEDDINGS_PATH) -> dict[str, StoredEmbedding]:
    if not path.is_file():
        return {}
    stored = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        stored[row["incident_id"]] = StoredEmbedding(
            incident_id=row["incident_id"],
            model=row["model"],
            text_sha256=row["text_sha256"],
            vector=row["vector"],
        )
    return stored


def seed_corpus_if_empty(store, model: str, path: Path = EMBEDDINGS_PATH) -> int:
    """Load the committed corpus at startup when the table is empty, so a clean `docker compose up`
    has working retrieval without Ollama or a manual step (issue #19).

    Never fatal. The service is designed to start with the database down, and an unusable corpus is
    a reason to log loudly and answer without retrieval, not to refuse to start."""
    try:
        records = load_corpus()
        vectors = vectors_for(records, model, path)
        seeded = store.seed_incidents(records, vectors)
    except Exception as exc:  # noqa: BLE001 - startup must survive anything here
        log.warning("corpus not seeded (%s: %s); retrieval will report an empty corpus", type(exc).__name__, exc)
        return 0
    if seeded:
        log.info("seeded %d incidents from %s; no embedding model was called", seeded, path.name)
    return seeded


def vectors_for(records: list[IncidentRecord], model: str, path: Path = EMBEDDINGS_PATH) -> list[list[float]]:
    """The committed vector for every record, or an error naming exactly what is wrong. Never
    returns a vector for text that has changed, and never silently skips a record."""
    stored = load_embeddings(path)
    if not stored:
        raise StaleEmbeddings(f"no precomputed embeddings at {path}; run corpus/ingest.py --write-embeddings")
    missing, changed, other_model, wrong_size = [], [], [], []
    vectors = []
    for record in records:
        found = stored.get(record.incident_id)
        if found is None:
            missing.append(record.incident_id)
            continue
        if found.model != model:
            other_model.append(f"{record.incident_id} ({found.model})")
        if found.text_sha256 != text_digest(document_text(record)):
            changed.append(record.incident_id)
        if len(found.vector) != EMBEDDING_DIMENSIONS:
            wrong_size.append(f"{record.incident_id} ({len(found.vector)})")
        vectors.append(found.vector)
    problems = []
    for label, ids in (("missing", missing), ("edited since embedding", changed),
                       ("embedded with another model", other_model), ("wrong dimension", wrong_size)):
        if ids:
            problems.append(f"{label}: {', '.join(sorted(ids)[:5])}{' ...' if len(ids) > 5 else ''}")
    if problems:
        raise StaleEmbeddings(
            "precomputed embeddings are out of date (" + "; ".join(problems)
            + "); regenerate with corpus/ingest.py --write-embeddings"
        )
    return vectors
