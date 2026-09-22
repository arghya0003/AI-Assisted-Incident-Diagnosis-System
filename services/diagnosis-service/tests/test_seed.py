"""Issue #19: the committed corpus embeddings, and the startup seed that uses them."""

import json

import pytest

from app.corpus import load_corpus
from app.retrieval import EMBEDDING_DIMENSIONS, document_text
from app.seed import (
    EMBEDDINGS_PATH,
    StaleEmbeddings,
    load_embeddings,
    seed_corpus_if_empty,
    text_digest,
    vectors_for,
    write_embeddings,
)

CORPUS = load_corpus()
MODEL = "nomic-embed-text"


class FakeStore:
    def __init__(self, already_holds=0, fails=False):
        self.already_holds = already_holds
        self.fails = fails
        self.seeded = None

    def seed_incidents(self, records, vectors):
        if self.fails:
            raise RuntimeError("cannot reach TimescaleDB at timescaledb:5432")
        self.seeded = (records, vectors)
        return 0 if self.already_holds else len(records)


def test_every_incident_has_a_committed_vector():
    """The point of the file: a clean checkout must have a usable corpus with no Ollama."""
    vectors = vectors_for(CORPUS, MODEL)
    assert len(vectors) == len(CORPUS)
    assert all(len(vector) == EMBEDDING_DIMENSIONS for vector in vectors)


def test_committed_vectors_match_the_text_they_were_made_from():
    """Catches a corpus edit that was committed without regenerating the embeddings."""
    stored = load_embeddings()
    stale = [r.incident_id for r in CORPUS if stored[r.incident_id].text_sha256 != text_digest(document_text(r))]
    assert stale == [], f"regenerate with corpus/ingest.py --write-embeddings: {stale}"


def test_an_edited_incident_is_refused_rather_than_embedded_wrongly(tmp_path):
    rows = [json.loads(line) for line in EMBEDDINGS_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows[0]["text_sha256"] = "0" * 64
    path = tmp_path / "embeddings.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    with pytest.raises(StaleEmbeddings, match="edited since embedding"):
        vectors_for(CORPUS, MODEL, path)


def test_a_missing_incident_is_refused(tmp_path):
    rows = [json.loads(line) for line in EMBEDDINGS_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    path = tmp_path / "embeddings.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows[1:]), encoding="utf-8")
    with pytest.raises(StaleEmbeddings, match="missing"):
        vectors_for(CORPUS, MODEL, path)


def test_another_embedding_model_is_refused(tmp_path):
    path = tmp_path / "embeddings.jsonl"
    write_embeddings(CORPUS, [[0.0] * EMBEDDING_DIMENSIONS] * len(CORPUS), "some-other-model", path)
    with pytest.raises(StaleEmbeddings, match="another model"):
        vectors_for(CORPUS, MODEL, path)


def test_write_then_read_round_trips(tmp_path):
    path = tmp_path / "embeddings.jsonl"
    vectors = [[0.5] * EMBEDDING_DIMENSIONS for _ in CORPUS]
    assert write_embeddings(CORPUS, vectors, MODEL, path) == len(CORPUS)
    assert vectors_for(CORPUS, MODEL, path) == vectors


def test_startup_seeds_an_empty_table():
    store = FakeStore()
    assert seed_corpus_if_empty(store, MODEL) == len(CORPUS)
    records, vectors = store.seeded
    assert len(records) == len(vectors) == len(CORPUS)


def test_startup_leaves_a_populated_table_alone():
    """Ingestion owns a populated corpus; a seed that overwrote one could undo a re-ingest."""
    assert seed_corpus_if_empty(FakeStore(already_holds=61), MODEL) == 0


def test_startup_survives_a_database_outage():
    """The service deliberately starts with the database down; an unusable corpus must not
    stop it, only be logged."""
    assert seed_corpus_if_empty(FakeStore(fails=True), MODEL) == 0


def test_startup_survives_missing_embeddings(tmp_path):
    assert seed_corpus_if_empty(FakeStore(), MODEL, tmp_path / "absent.jsonl") == 0
