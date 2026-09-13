#!/usr/bin/env python3
"""
Phase 0 LLM benchmark for Member 3.

Answers the three questions that decide whether phi4-mini is a real choice or a
guess, on THIS machine (RTX 3050 Ti, 4 GB VRAM):

  1. How fast is it?            tokens/sec on a realistic prompt
  2. Does it fit in VRAM?       or is Ollama silently offloading to CPU
  3. Is its JSON trustworthy?   valid, schema-conforming output N times out of N

Stdlib only - no pip install needed, so this runs before requirements.txt exists.

Usage:
    python services/diagnosis-service/scripts/phase0_llm_bench.py
    python services/diagnosis-service/scripts/phase0_llm_bench.py --model qwen3:4b --runs 10
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request

# A prompt deliberately shaped like the real Phase 6 one: anomaly facts, scored
# candidates with evidence IDs, retrieved incidents, and a strict output schema.
# Roughly 700-900 tokens - enough to be representative without being the worst case.
SYSTEM = (
    "You are an incident diagnosis assistant. You are given an anomaly, a list of "
    "candidate root-cause services already scored by a deterministic pipeline, and "
    "similar past incidents. Your job is to explain and rank - not to invent. "
    "Cite ONLY evidence_ids that appear in the input. Respond with JSON only."
)

USER = """ANOMALY
  anomaly_id: anom-0001
  services: [front-end, catalogue]
  metrics: [latency_p99_ms, error_rate]
  severity: high
  t_onset: 2026-08-12T20:44:50Z
  observed: catalogue latency_p99_ms = 598.4 (baseline 142.3, 4.2x)
  evidence_id: ev-0001

SCORED CANDIDATES
  1. catalogue          score 0.81
       deploy_proximity 0.74  (dep-2026-08-12-0007, 3 min before onset)  ev-0002
       graph_proximity  0.50  (1 hop downstream of front-end)            ev-0003
       co_anomaly       1.00  (also anomalous in window)                 ev-0004
  2. catalogue-db       score 0.34
       deploy_proximity 0.00  (no recent deploy)
       graph_proximity  0.33  (2 hops downstream)                        ev-0005
       co_anomaly       0.00
  3. front-end          score 0.29
       deploy_proximity 0.00  (no recent deploy)
       graph_proximity  1.00  (the anomalous service itself)             ev-0006
       co_anomaly       0.00

DEPLOY DIFFS
  dep-2026-08-12-0007  catalogue v1.4.2  commit a1b2c3d
    config_diff: db.pool.maxActive 50 -> 5

SIMILAR PAST INCIDENTS
  incident-0042  "Catalogue latency spike after pool resize"
    root cause: connection pool max lowered in config, requests queued on acquire
    resolution: reverted the pool setting
    similarity 0.88                                                      ev-0007

ALLOWED ACTIONS (pick exactly one, with a real target id)
  rollback_deploy:<deploy_id> | restart_service:<service> | scale_service:<service> | no_action

OUTPUT SCHEMA - return exactly this shape, nothing else:
{"hypotheses":[{"rank":1,"cause":"<one sentence>","confidence":0.0,
  "evidence_ids":["ev-..."],"proposed_action":"<action>:<target>"}]}
"""

ALLOWED_ACTION_PREFIXES = ("rollback_deploy:", "restart_service:", "scale_service:", "no_action")


def post(url: str, payload: dict, timeout: int = 300) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def get(url: str, timeout: int = 10) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def validate(text: str) -> tuple[bool, str]:
    """The same validation Phase 6 will do with Pydantic, written by hand."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return False, f"not valid JSON: {exc}"

    if not isinstance(data, dict) or "hypotheses" not in data:
        return False, "missing top-level 'hypotheses'"
    hyps = data["hypotheses"]
    if not isinstance(hyps, list) or not hyps:
        return False, "'hypotheses' is not a non-empty list"

    for i, h in enumerate(hyps):
        if not isinstance(h, dict):
            return False, f"hypothesis[{i}] is not an object"
        for field in ("rank", "cause", "confidence", "evidence_ids", "proposed_action"):
            if field not in h:
                return False, f"hypothesis[{i}] missing '{field}'"
        if not isinstance(h["rank"], int):
            return False, f"hypothesis[{i}].rank is not an int"
        if not isinstance(h["confidence"], (int, float)) or not 0 <= h["confidence"] <= 1:
            return False, f"hypothesis[{i}].confidence not a float in 0..1"
        if not isinstance(h["evidence_ids"], list) or not h["evidence_ids"]:
            return False, f"hypothesis[{i}].evidence_ids not a non-empty list"
        if not str(h["proposed_action"]).startswith(ALLOWED_ACTION_PREFIXES):
            return False, f"hypothesis[{i}].proposed_action outside the allowed vocabulary: {h['proposed_action']!r}"

    return True, "ok"


def hallucinated_ids(text: str, allowed: set[str]) -> list[str]:
    """Preview of the Phase 7 guardrail: which cited IDs were never in the prompt."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    bad: list[str] = []
    for h in data.get("hypotheses", []):
        if isinstance(h, dict):
            for eid in h.get("evidence_ids", []) or []:
                if str(eid) not in allowed:
                    bad.append(str(eid))
    return bad


def gpu_placement(model: str) -> str:
    """`ollama ps` reports how much of the model actually landed on the GPU."""
    try:
        out = subprocess.run(
            ["ollama", "ps"], capture_output=True, text=True, timeout=15
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return "unknown (could not run 'ollama ps')"
    for line in out.splitlines():
        if model.split(":")[0] in line:
            return line.strip()
    return "model not resident (it may have unloaded already)"


def vram_used_mib() -> str:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        return out or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown (nvidia-smi unavailable)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:11434")
    ap.add_argument("--model", default="phi4-mini")
    ap.add_argument("--embed-model", default="nomic-embed-text")
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--num-ctx", type=int, default=8192)
    args = ap.parse_args()

    print(f"Phase 0 LLM benchmark - model={args.model} runs={args.runs} num_ctx={args.num_ctx}\n")

    # --- reachability ----------------------------------------------------
    try:
        tags = get(f"{args.url}/api/tags")
    except (urllib.error.URLError, OSError) as exc:
        print(f"FAIL  Ollama not reachable at {args.url}: {exc}")
        print("      Install Ollama and make sure it is running, then re-run.")
        return 1
    installed = {m.get("name", "") for m in tags.get("models", [])}
    print(f"OK    Ollama reachable. Installed: {', '.join(sorted(installed)) or '(none)'}")

    for want in (args.model, args.embed_model):
        if not any(n == want or n.startswith(want + ":") for n in installed):
            print(f"FAIL  model '{want}' not pulled.  Run:  ollama pull {want}")
            return 1
    print(f"OK    '{args.model}' and '{args.embed_model}' both present\n")

    # --- embedding model -------------------------------------------------
    t0 = time.perf_counter()
    try:
        emb = post(f"{args.url}/api/embeddings",
                   {"model": args.embed_model, "prompt": "catalogue p99 latency spike after deploy"},
                   timeout=120)
        dim = len(emb.get("embedding", []))
        print(f"OK    embeddings: {dim} dims in {(time.perf_counter()-t0)*1000:.0f} ms")
        if dim != 768:
            print(f"WARN  expected 768 dims for nomic-embed-text, got {dim} - update the schema in PLAN.md Phase 2")
    except (urllib.error.URLError, OSError) as exc:
        print(f"FAIL  embedding call failed: {exc}")
        return 1

    # --- warm-up (first call pays model load; don't let it pollute timings)
    print(f"\nWarming up {args.model} (first load can take 30s+)...")
    t0 = time.perf_counter()
    try:
        post(f"{args.url}/api/chat", {
            "model": args.model,
            "messages": [{"role": "user", "content": "Reply with the single word: ready"}],
            "stream": False,
            "options": {"num_ctx": args.num_ctx},
        })
    except (urllib.error.URLError, OSError) as exc:
        print(f"FAIL  generation call failed: {exc}")
        return 1
    print(f"OK    warm-up done in {time.perf_counter()-t0:.1f}s")
    print(f"      VRAM now (used/total): {vram_used_mib()}")
    print(f"      ollama ps: {gpu_placement(args.model)}")
    print("      ^ if that line says any %CPU, the model is NOT fully on the GPU and will be slow\n")

    # --- the real benchmark ----------------------------------------------
    allowed = {f"ev-{i:04d}" for i in range(1, 8)} | {
        "anom-0001", "dep-2026-08-12-0007", "incident-0042"
    }

    latencies: list[float] = []
    tps: list[float] = []
    valid = 0
    failures: list[str] = []
    hallucinations = 0

    print(f"Running {args.runs} JSON-mode generations on a realistic prompt...")
    for i in range(1, args.runs + 1):
        t0 = time.perf_counter()
        try:
            r = post(f"{args.url}/api/chat", {
                "model": args.model,
                "messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": USER},
                ],
                "stream": False,
                "format": "json",
                "options": {"num_ctx": args.num_ctx, "temperature": 0.1},
            })
        except (urllib.error.URLError, OSError) as exc:
            print(f"  run {i:2d}: TRANSPORT ERROR {exc}")
            failures.append(f"run {i}: transport error")
            continue

        elapsed = time.perf_counter() - t0
        latencies.append(elapsed)

        eval_count = r.get("eval_count") or 0
        eval_ns = r.get("eval_duration") or 0
        if eval_count and eval_ns:
            tps.append(eval_count / (eval_ns / 1e9))

        content = (r.get("message") or {}).get("content", "")
        good, why = validate(content)
        halluc = hallucinated_ids(content, allowed)
        if halluc:
            hallucinations += 1

        if good:
            valid += 1
            extra = f"  hallucinated_ids={halluc}" if halluc else ""
            print(f"  run {i:2d}: valid    {elapsed:5.1f}s  {eval_count:4d} tok{extra}")
        else:
            failures.append(f"run {i}: {why}")
            print(f"  run {i:2d}: INVALID  {elapsed:5.1f}s  - {why}")

    # --- report ----------------------------------------------------------
    print("\n" + "=" * 62)
    print("RESULTS - paste these into services/diagnosis-service/README.md")
    print("=" * 62)
    print(f"  model                 {args.model}")
    print(f"  runs                  {args.runs}")
    if latencies:
        print(f"  latency p50           {statistics.median(latencies):.1f}s")
        print(f"  latency min / max     {min(latencies):.1f}s / {max(latencies):.1f}s")
    if tps:
        print(f"  generation speed      {statistics.median(tps):.1f} tok/s (median)")
    print(f"  valid JSON            {valid}/{args.runs}")
    print(f"  hallucinated evidence {hallucinations}/{args.runs} runs cited an unknown ID")
    print(f"  VRAM (used/total)     {vram_used_mib()}")
    print(f"  GPU placement         {gpu_placement(args.model)}")
    if failures:
        print("\n  validation failures:")
        for f in failures:
            print(f"    - {f}")

    print("\nHow to read this:")
    if valid == args.runs:
        print("  * 10/10 valid JSON: phi4-mini is a defensible choice. Keep the")
        print("    validate-and-retry loop anyway - 10 runs is not a guarantee.")
    elif valid >= args.runs * 0.7:
        print("  * Mostly valid: workable, and exactly why Phase 6 has a 3-attempt")
        print("    retry plus a deterministic fallback. Record this rate as a result.")
    else:
        print("  * Under 70% valid: try a stricter prompt or an explicit JSON schema")
        print("    first. If it does not improve, evaluate qwen3:4b before committing.")
    if hallucinations:
        print(f"  * {hallucinations} run(s) cited evidence IDs that were never in the prompt.")
        print("    This is the exact failure the Phase 7 guardrail exists to catch, and")
        print("    it is a good number to quote in the report as justification.")
    if tps and statistics.median(tps) < 10:
        print("  * Under 10 tok/s suggests CPU offload. Check the 'GPU placement' line;")
        print("    if it shows any %CPU, reduce num_ctx or try a smaller quantisation.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
