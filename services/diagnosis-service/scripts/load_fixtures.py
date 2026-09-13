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
            cur.execute("DELETE FROM anomalies WHERE source = 'fixture'")
            removed = cur.rowcount
            for fixture in fixtures:
                if save_anomaly(cur, fixture.event, fixture.raw, source="fixture") != "inserted":
                    raise SystemExit(
                        f"{fixture.path.name}: {fixture.event.anomaly_id} already exists as a "
                        "non-fixture row; nothing was changed"
                    )
    finally:
        conn.close()

    print(f"removed {removed} fixture row(s), loaded {len(fixtures)}")
    for fixture in fixtures:
        truth = fixture.meta.ground_truth_service or "none"
        print(f"  {fixture.event.anomaly_id}  truth={truth:<10} {fixture.meta.description}")


if __name__ == "__main__":
    main()
