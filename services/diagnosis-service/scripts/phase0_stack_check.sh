#!/usr/bin/env bash
# Phase 0 verification for Member 3 (diagnosis-service).
#
# Checks that every upstream dependency this slice needs actually exists and
# has the shape CONTRACTS.md claims. Read-only: creates nothing, changes nothing.
#
# Run from the repo root:   bash services/diagnosis-service/scripts/phase0_stack_check.sh
#
# Exit code is always 0 - this is a report, not a gate. Read the output.

set -uo pipefail

PASS=0
FAIL=0
WARN=0

ok()   { printf '  [ OK ]   %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  [FAIL]   %s\n' "$1"; FAIL=$((FAIL+1)); }
warn() { printf '  [WARN]   %s\n' "$1"; WARN=$((WARN+1)); }
head_() { printf '\n=== %s ===\n' "$1"; }

PSQL="docker compose exec -T timescaledb psql -U postgres -d metrics -tAc"
KT="docker compose exec -T kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092"

# ---------------------------------------------------------------- 1. Docker
head_ "1. Docker engine"
if ! docker info >/dev/null 2>&1; then
  bad "Docker daemon not reachable. Start Docker Desktop and wait for the whale icon to go steady, then re-run."
  printf '\nStopping here - every remaining check needs the daemon.\n'
  exit 0
fi
ok "Docker daemon reachable"

# ---------------------------------------------------------- 2. M1's stack up
head_ "2. M1 stack health"
RUNNING=$(docker compose ps --status running --format '{{.Name}}' 2>/dev/null | grep -c . || true)
if [ "${RUNNING:-0}" -eq 0 ]; then
  bad "No containers running. Start the stack:  docker compose up -d"
  printf '\nStopping here - the data checks below need the stack.\n'
  exit 0
fi
ok "$RUNNING containers running (M1 documents 22 as the full set)"
if [ "${RUNNING:-0}" -lt 20 ]; then
  warn "Fewer than 20 running. Check for crash-loopers:  docker compose ps -a"
fi

RESTARTERS=$(docker compose ps -a --format '{{.Name}} {{.Status}}' 2>/dev/null \
  | grep -iE 'restarting|exited \([1-9]' || true)
if [ -n "$RESTARTERS" ]; then
  warn "Containers not in a clean state:"
  printf '           %s\n' "$RESTARTERS"
else
  ok "No restarting or failed-exit containers"
fi

# ------------------------------------------------------ 3. TimescaleDB tables
head_ "3. Upstream tables (what /analyze reads from)"
if ! $PSQL "SELECT 1" >/dev/null 2>&1; then
  bad "Cannot reach the metrics database. Is the timescaledb container healthy?"
else
  ok "Connected to the metrics database"

  for T in metrics deploys evidence fault_scenarios; do
    if $PSQL "SELECT to_regclass('public.$T') IS NOT NULL" 2>/dev/null | grep -q '^t$'; then
      N=$($PSQL "SELECT count(*) FROM $T" 2>/dev/null | tr -d '[:space:]')
      ok "table '$T' exists, ${N:-?} rows"
      # metrics and deploys MUST have data or scoring has nothing to score
      if [ "$T" = "metrics" ] && [ "${N:-0}" -eq 0 ]; then
        bad "  -> 'metrics' is empty. metrics-bridge/metrics-sink are not flowing; Phase 4 scoring will have no data."
      fi
      if [ "$T" = "deploys" ] && [ "${N:-0}" -eq 0 ]; then
        warn "  -> 'deploys' is empty. deploy-emitter fabricates one every 2 min; wait a few minutes or POST /deploys on :5000."
      fi
    else
      bad "table '$T' MISSING. Volume predates its migration - re-run the SQL or 'docker compose down -v' (destroys data)."
    fi
  done

  # M3's own tables should NOT exist yet - Phase 2 creates them
  for T in anomalies incidents hypotheses; do
    if $PSQL "SELECT to_regclass('public.$T') IS NOT NULL" 2>/dev/null | grep -q '^t$'; then
      warn "table '$T' already exists - Phase 2's 005_diagnosis.sql was applied already?"
    fi
  done

  # Freshness: stale metrics look like present data but break time-window queries
  LAG=$($PSQL "SELECT round(extract(epoch from (now() - max(time)))) FROM metrics" 2>/dev/null | tr -d '[:space:]')
  if [ -n "${LAG:-}" ] && [ "$LAG" != "" ]; then
    if [ "$LAG" -lt 60 ] 2>/dev/null; then
      ok "newest metric sample is ${LAG}s old (pipeline is live)"
    else
      warn "newest metric sample is ${LAG}s old - ingestion may be stalled"
    fi
  fi

  # pgvector: decides Phase 5's storage approach
  if $PSQL "SELECT count(*) FROM pg_available_extensions WHERE name='vector'" 2>/dev/null | grep -q '^1$'; then
    ok "pgvector AVAILABLE -> Phase 5 can use vector(768) and the <=> operator"
  else
    warn "pgvector NOT available in this image -> Phase 5 uses the NumPy brute-force path (fine at 50-150 records)"
  fi
fi

# ------------------------------------------------------------- 4. Kafka topics
head_ "4. Kafka topics"
TOPICS=$($KT --list 2>/dev/null || true)
if [ -z "$TOPICS" ]; then
  bad "Could not list topics. Is the kafka container healthy?"
else
  for T in metrics.raw deploys.events anomalies.detected; do
    if printf '%s\n' "$TOPICS" | grep -qx "$T"; then
      PARTS=$($KT --describe --topic "$T" 2>/dev/null | grep -o 'PartitionCount: *[0-9]*' | grep -o '[0-9]*' || true)
      ok "topic '$T' exists (PartitionCount: ${PARTS:-?})"
    else
      if [ "$T" = "anomalies.detected" ]; then
        bad "topic 'anomalies.detected' MISSING - it is not in kafka-init, so it only appears once M2 first publishes. See PLAN.md open item 4."
      else
        bad "topic '$T' MISSING - kafka-init did not run?"
      fi
    fi
  done

  # Has M2 actually emitted anything? This is the input to my whole slice.
  if printf '%s\n' "$TOPICS" | grep -qx "anomalies.detected"; then
    printf '\n  Sampling anomalies.detected (5s timeout)...\n'
    SAMPLE=$(docker compose exec -T kafka /opt/kafka/bin/kafka-console-consumer.sh \
      --bootstrap-server kafka:9092 --topic anomalies.detected \
      --from-beginning --max-messages 3 --timeout-ms 5000 2>/dev/null || true)
    if [ -n "$SAMPLE" ]; then
      ok "M2 has published real anomalies. Sample:"
      printf '           %s\n' "$SAMPLE"
    else
      warn "topic exists but is empty - M2 has not detected anything yet. Inject a fault: curl -XPOST localhost:5001/inject ..."
    fi
  fi
fi

# -------------------------------------------------------------- 5. Ollama host
head_ "5. Ollama (host side)"
if curl -fsS --max-time 5 http://localhost:11434/api/tags >/dev/null 2>&1; then
  ok "Ollama reachable at localhost:11434"
  MODELS=$(curl -fsS --max-time 5 http://localhost:11434/api/tags 2>/dev/null \
    | tr ',' '\n' | grep -o '"name":"[^"]*"' | cut -d'"' -f4 || true)
  printf '           installed: %s\n' "$(printf '%s ' $MODELS)"
  for M in phi4-mini nomic-embed-text; do
    if printf '%s\n' "$MODELS" | grep -q "^${M}"; then
      ok "model '$M' pulled"
    else
      bad "model '$M' NOT pulled. Run:  ollama pull $M"
    fi
  done
else
  bad "Ollama not reachable on localhost:11434. Install it, then 'ollama serve' (Windows: it runs as a tray app)."
fi

# ------------------------------------------------- 6. Ollama from a container
# This is the check that actually matters for Phase 1: diagnosis-service runs
# INSIDE Docker and must reach Ollama on the host. On Windows, Ollama binds
# 127.0.0.1 by default, which containers cannot reach even via host.docker.internal.
head_ "6. Ollama reachable FROM a container (the Phase 1 gotcha)"
if docker run --rm --add-host host.docker.internal:host-gateway curlimages/curl:latest \
     -fsS --max-time 5 http://host.docker.internal:11434/api/tags >/dev/null 2>&1; then
  ok "A container can reach Ollama via host.docker.internal:11434"
else
  bad "A container CANNOT reach Ollama. Set OLLAMA_HOST=0.0.0.0 in Windows env vars and restart Ollama."
  printf '           Without this, Phase 1 onward fails with connection-refused from inside the container.\n'
fi

# -------------------------------------------------------------------- summary
head_ "Summary"
printf '  pass: %d   fail: %d   warn: %d\n\n' "$PASS" "$FAIL" "$WARN"
if [ "$FAIL" -eq 0 ]; then
  printf '  Phase 0 clear. Record the numbers above plus the LLM benchmark in README.md, then start Phase 1.\n'
else
  printf '  %d blocking item(s). Fix those before Phase 1 - they invalidate design choices downstream.\n' "$FAIL"
fi
printf '\n'
