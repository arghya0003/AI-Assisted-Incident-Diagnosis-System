"""Embed corpus/incidents/*.md with nomic-embed-text and load them into the `incidents` table.

Idempotent and re-runnable: every file is validated and embedded before the database is touched,
then all records are upserted and rows whose file no longer exists are deleted, in one
transaction. A failure part-way leaves the table as it was.

Run inside the compose network (TimescaleDB and Ollama both reachable):
    bash services/diagnosis-service/scripts/test_in_docker.sh --ingest

Two flags avoid needing Ollama at all (issue #19):
    python corpus/ingest.py --offline            # load the committed vectors, no Ollama
    python corpus/ingest.py --write-embeddings   # regenerate them after editing the corpus
The service seeds an empty table from the same file at startup, so a clean `docker compose up`
has a working corpus with no manual step.
"""

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Defaults for running from the host instead of inside Docker.
os.environ.setdefault("PG_HOST", "localhost")
os.environ.setdefault("OLLAMA_URL", "http://localhost:11434")

from app.corpus import load_corpus  # noqa: E402
from app.db import connect, count_incidents, delete_incidents_except, upsert_incident  # noqa: E402
from app.ollama import OllamaClient  # noqa: E402
from app.retrieval import EMBEDDING_DIMENSIONS, document_text  # noqa: E402
from app.seed import EMBEDDINGS_PATH, vectors_for, write_embeddings  # noqa: E402
from app.settings import Settings  # noqa: E402

BATCH_SIZE = 16


def embed_with_ollama(records, settings) -> list[list[float]]:
    client = OllamaClient(settings.ollama_url, timeout_seconds=settings.ollama_timeout_seconds)
    vectors: list[list[float]] = []
    for start in range(0, len(records), BATCH_SIZE):
        batch = records[start : start + BATCH_SIZE]
        vectors.extend(client.embed([document_text(record) for record in batch], settings.embed_model))
    return vectors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--offline", action="store_true", help="use the committed vectors instead of calling Ollama")
    parser.add_argument("--write-embeddings", action="store_true",
                        help="embed with Ollama and write corpus/embeddings.jsonl, then load as usual")
    args = parser.parse_args()

    settings = Settings.from_env()
    records = load_corpus()

    if args.offline:
        vectors = vectors_for(records, settings.embed_model)
        print(f"using {len(vectors)} precomputed embeddings from {EMBEDDINGS_PATH.name}; Ollama not called")
    else:
        vectors = embed_with_ollama(records, settings)
    if args.write_embeddings:
        written = write_embeddings(records, vectors, settings.embed_model)
        print(f"wrote {written} embeddings to {EMBEDDINGS_PATH}")
    wrong = {r.incident_id: len(v) for r, v in zip(records, vectors) if len(v) != EMBEDDING_DIMENSIONS}
    if wrong:
        raise SystemExit(f"expected {EMBEDDING_DIMENSIONS}-dim embeddings, got {wrong}; nothing was changed")

    conn = connect(settings)
    try:
        with conn, conn.cursor() as cur:
            for record, vector in zip(records, vectors):
                upsert_incident(cur, record, vector)
            removed = delete_incidents_except(cur, [record.incident_id for record in records])
            total = count_incidents(cur)
    finally:
        conn.close()

    source_note = "precomputed" if args.offline else settings.embed_model
    print(f"ingested {len(records)} incidents with {source_note}; removed {removed} stale; table now holds {total}")
    for source, count in sorted(Counter(r.source for r in records).items()):
        print(f"  {source:<18} {count}")
    for fault_type, count in sorted(Counter(r.fault_type or "none" for r in records).items()):
        print(f"    {fault_type:<20} {count}")


if __name__ == "__main__":
    main()
