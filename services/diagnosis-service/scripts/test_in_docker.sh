#!/usr/bin/env bash
# Run the test suite, and optionally corpus ingestion, fixture loading and the retrieval
# comparison, from inside the compose network.
#
# Why: the DB integration tests need TimescaleDB. From the host they connect to localhost:5432,
# which a local PostgreSQL install can occupy instead of Docker's (the case on the Phase 2 dev
# machine), and then they skip. Inside the network, timescaledb:5432 is unambiguous.
#
# Usage, with the stack up. Flags combine; the tests always run first, and a failure stops the rest.
#   bash services/diagnosis-service/scripts/test_in_docker.sh              # tests
#   bash services/diagnosis-service/scripts/test_in_docker.sh --ingest     # + embed and load corpus/incidents
#   bash services/diagnosis-service/scripts/test_in_docker.sh --fixtures   # + load fixtures into anomalies
#   bash services/diagnosis-service/scripts/test_in_docker.sh --compare    # + vector vs hybrid retrieval
#   bash services/diagnosis-service/scripts/test_in_docker.sh --eval       # + LLM runs per fixture (EVAL_RUNS, default 10)
# --ingest, --compare and --eval need Ollama running on the host with OLLAMA_HOST=0.0.0.0.
set -euo pipefail
export MSYS_NO_PATHCONV=1  # Git Bash would otherwise rewrite /src into a Windows path

INGEST=0 FIXTURES=0 COMPARE=0 EVAL=0
for arg in "$@"; do
  case "$arg" in
    --ingest) INGEST=1 ;;
    --fixtures) FIXTURES=1 ;;
    --compare) COMPARE=1 ;;
    --eval) EVAL=1 ;;
    *) echo "unknown option: $arg (expected --ingest, --fixtures, --compare, --eval)" >&2; exit 2 ;;
  esac
done

SERVICE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SERVICE_DIR/../.."

docker compose build --quiet diagnosis-service
# --images also lists the images of depends_on services (kafka, timescaledb); keep only ours.
IMAGE="$(docker compose config --images diagnosis-service | tr -d '\r' | grep -m1 'diagnosis-service$')"
NETWORK="$(docker network ls --format '{{.Name}}' | grep -m1 'diagnosis-net$')"
# Git Bash needs the Windows form of the path for a bind mount; elsewhere pwd is already right.
MOUNT="$(cd "$SERVICE_DIR" && (pwd -W 2>/dev/null || pwd))"

CMD="pip install -q --root-user-action=ignore -r requirements-dev.txt && python -m pytest -q -rs -p no:cacheprovider"
if [ "$INGEST" = 1 ]; then CMD="$CMD && python corpus/ingest.py"; fi
if [ "$FIXTURES" = 1 ]; then CMD="$CMD && python scripts/load_fixtures.py"; fi
if [ "$COMPARE" = 1 ]; then CMD="$CMD && python scripts/compare_retrieval.py"; fi
if [ "$EVAL" = 1 ]; then CMD="$CMD && python scripts/eval_llm.py --runs ${EVAL_RUNS:-10}"; fi

docker run --rm --network "$NETWORK" -v "$MOUNT:/src" -w /src \
  --add-host host.docker.internal:host-gateway \
  -e TEST_PG_HOST=timescaledb -e PG_HOST=timescaledb \
  -e OLLAMA_URL=http://host.docker.internal:11434 -e PYTHONDONTWRITEBYTECODE=1 \
  "$IMAGE" sh -c "$CMD"
