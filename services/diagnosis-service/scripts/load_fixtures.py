"""Load fixtures/anomalies/*.json into the anomalies table with source='fixture'.

Lets POST /analyze be exercised against known scenarios without waiting for M2. Existing
fixture rows are replaced, so editing a fixture and re-running picks up the change. Real M2
rows (source='kafka') are never touched.

Run from services/diagnosis-service/ with the dev venv active:
    python scripts/load_fixtures.py            # load or refresh
    python scripts/load_fixtures.py --remove   # delete fixture rows only
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Run from the host, through the 5432 port docker-compose publishes.
os.environ.setdefault("PG_HOST", "localhost")

from app.db import connect, save_anomaly  # noqa: E402
from app.fixtures import load_fixtures  # noqa: E402
from app.settings import Settings  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--remove", action="store_true", help="only delete fixture rows")
    args = parser.parse_args()

    # Validate every file before touching the database.
    fixtures = [] if args.remove else load_fixtures()

    conn = connect(Settings.from_env())
    try:
        with conn, conn.cursor() as cur:
            # Removing only the anomalies would leave their stored /analyze runs behind. A
            # reloaded fixture keeps its id, so the response cache could then serve an answer
            # computed from the *previous* version of that fixture. Take the runs with it.
            cur.execute("DELETE FROM anomalies WHERE source = 'fixture' RETURNING anomaly_id")
            gone = [anomaly_id for (anomaly_id,) in cur.fetchall()]
            removed = len(gone)
            if gone:
                cur.execute(
                    "DELETE FROM hypotheses WHERE analysis_id IN "
                    "(SELECT analysis_id FROM analyses WHERE anomaly_id = ANY(%s))",
                    (gone,),
                )
                orphaned_hypotheses = cur.rowcount
                cur.execute("DELETE FROM analyses WHERE anomaly_id = ANY(%s)", (gone,))
                orphaned_analyses = cur.rowcount
                cur.execute("DELETE FROM evidence WHERE incident_id = ANY(%s)", (gone,))
                orphaned_evidence = cur.rowcount
            else:
                orphaned_hypotheses = orphaned_analyses = orphaned_evidence = 0
            for fixture in fixtures:
                # Related anomalies go in too, so /candidates sees the co-anomalies. Context deploys
                # do not: `deploys` is M1's table, so DB-backed scoring of a fixture has no deploy
                # signal and can rank differently from tests/test_scoring.py.
                events = [(fixture.event, fixture.raw)] + [
                    (other, other.model_dump(mode="json")) for other in fixture.meta.context.related_anomalies
                ]
                for event, raw in events:
                    # Keep the fixture's own detector where it has one, so a stored fixture looks
                    # like the event it was copied from.
                    detector = event.detector or "fixture"
                    if save_anomaly(cur, event, raw, detector=detector, source="fixture") != "inserted":
                        raise SystemExit(
                            f"{fixture.path.name}: {event.anomaly_id} already exists as a "
                            "non-fixture row; nothing was changed"
                        )
    finally:
        conn.close()

    print(
        f"removed {removed} fixture row(s) with {orphaned_analyses} stored analysis/analyses, "
        f"{orphaned_hypotheses} hypothesis/hypotheses and {orphaned_evidence} evidence row(s); "
        f"loaded {len(fixtures)}"
    )
    for fixture in fixtures:
        truth = fixture.meta.ground_truth_service or "none"
        print(f"  {fixture.event.anomaly_id}  truth={truth:<10} {fixture.meta.description}")


if __name__ == "__main__":
    main()
