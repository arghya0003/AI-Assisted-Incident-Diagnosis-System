"""Compare pure-vector and hybrid retrieval on fixture anomalies (PLAN.md Phase 5 definition of done).

Prints the top-k incidents each mode retrieves for each fixture, for manual relevance judgement.
Fixture events are used directly, so they don't need to be loaded into the anomalies table.

Run inside the compose network after ingesting the corpus:
    bash services/diagnosis-service/scripts/test_in_docker.sh --ingest --compare
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("PG_HOST", "localhost")
os.environ.setdefault("OLLAMA_URL", "http://localhost:11434")

from app.db import PostgresAnomalyStore  # noqa: E402
from app.fixtures import load_fixtures  # noqa: E402
from app.graph import load_graph  # noqa: E402
from app.ollama import OllamaClient  # noqa: E402
from app.retrieval import Retriever, query_text  # noqa: E402
from app.settings import Settings  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("anomaly_ids", nargs="*", help="fixture ids to compare (default: all)")
    args = parser.parse_args()

    settings = Settings.from_env()
    store = PostgresAnomalyStore(settings)
    client = OllamaClient(settings.ollama_url, timeout_seconds=settings.ollama_timeout_seconds)
    graph = load_graph()

    def embed(texts):
        return client.embed(texts, settings.embed_model)

    fixtures = [f for f in load_fixtures() if not args.anomaly_ids or f.event.anomaly_id in args.anomaly_ids]
    retrievers = {
        mode: Retriever(embed, store, graph, mode=mode, top_k=settings.retrieval_top_k) for mode in ("vector", "hybrid")
    }
    for fixture in fixtures:
        meta = fixture.meta
        print(f"\n=== {fixture.event.anomaly_id}  truth={meta.ground_truth_service}  fault={meta.fault_type}")
        print(f"    {meta.description}")
        print(f"    query: {query_text(fixture.event)}")
        for mode, retriever in retrievers.items():
            result = retriever.retrieve(fixture.event)
            print(f"  {mode} ({result.status})")
            for incident in result.incidents:
                services = ",".join(incident.services) or "-"
                print(
                    f"    {incident.similarity:.3f}  {incident.incident_id}  [{incident.fault_type}]  "
                    f"root={services}  {incident.title}"
                )


if __name__ == "__main__":
    main()
