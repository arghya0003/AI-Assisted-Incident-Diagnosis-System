#!/usr/bin/env bash
# Run the test suite (and optionally load fixtures) from inside the compose network.
#
# Why: the DB integration tests need TimescaleDB. From the host they connect to localhost:5432,
# which a local PostgreSQL install can occupy instead of Docker's (the case on the Phase 2 dev
# machine), and then they skip. Inside the network, timescaledb:5432 is unambiguous.
#
# Usage, with the stack up:
#   bash services/diagnosis-service/scripts/test_in_docker.sh              # tests
#   bash services/diagnosis-service/scripts/test_in_docker.sh --fixtures   # tests, then load fixtures
set -euo pipefail
export MSYS_NO_PATHCONV=1  # Git Bash would otherwise rewrite /src into a Windows path

SERVICE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SERVICE_DIR/../.."

docker compose build --quiet diagnosis-service
# --images also lists the images of depends_on services (kafka, timescaledb); keep only ours.
IMAGE="$(docker compose config --images diagnosis-service | tr -d '\r' | grep -m1 'diagnosis-service$')"
NETWORK="$(docker network ls --format '{{.Name}}' | grep -m1 'diagnosis-net$')"
# Git Bash needs the Windows form of the path for a bind mount; elsewhere pwd is already right.
MOUNT="$(cd "$SERVICE_DIR" && (pwd -W 2>/dev/null || pwd))"

CMD="pip install -q --root-user-action=ignore -r requirements-dev.txt && python -m pytest -q -rs -p no:cacheprovider"
if [ "${1:-}" = "--fixtures" ]; then
  CMD="$CMD && python scripts/load_fixtures.py"
fi

docker run --rm --network "$NETWORK" -v "$MOUNT:/src" -w /src \
  -e TEST_PG_HOST=timescaledb -e PG_HOST=timescaledb -e PYTHONDONTWRITEBYTECODE=1 \
  "$IMAGE" sh -c "$CMD"
