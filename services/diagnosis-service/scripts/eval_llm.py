"""Phase 6 definition of done: run fixtures through the real pipeline (retrieval, scoring and
phi4-mini) several times each, and report schema validity, retry and fallback rates, latency and
prompt size.

Each fixture's scenario context (deploys and related anomalies) is used, so the LLM sees what
that scenario would have produced. Run inside the compose network, with Ollama on the host:
    bash services/diagnosis-service/scripts/test_in_docker.sh --eval        # EVAL_RUNS=10 by default
"""

import argparse
import math
import os
import statistics
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("PG_HOST", "localhost")
os.environ.setdefault("OLLAMA_URL", "http://localhost:11434")

from app.db import PostgresAnomalyStore  # noqa: E402
from app.fixtures import load_fixtures  # noqa: E402
from app.graph import load_graph  # noqa: E402
from app.models import AnalyzeResponse  # noqa: E402
from app.ollama import OllamaClient  # noqa: E402
from app.pipeline import DiagnosisPipeline, PipelineConfig, ollama_chat, ollama_embed  # noqa: E402
from app.scoring import ScoringConfig  # noqa: E402
from app.settings import Settings  # noqa: E402


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("anomaly_ids", nargs="*", help="fixture ids (default: all)")
    parser.add_argument("--runs", type=int, default=10)
    args = parser.parse_args()

    settings = Settings.from_env()
    client = OllamaClient(settings.ollama_url, timeout_seconds=settings.ollama_timeout_seconds)
    pipeline = DiagnosisPipeline(
        PostgresAnomalyStore(settings),
        ollama_embed(client, settings),
        ollama_chat(client, settings),
        load_graph(),
        ScoringConfig.from_settings(settings),
        PipelineConfig.from_settings(settings),
    )
    fixtures = [f for f in load_fixtures() if not args.anomaly_ids or f.event.anomaly_id in args.anomaly_ids]
    print(f"model={settings.llm_model} num_ctx={settings.llm_context_tokens} temperature={settings.llm_temperature} "
          f"attempts={settings.llm_max_attempts} runs={args.runs} fixtures={len(fixtures)}")

    warmup = pipeline.analyze_inputs(fixtures[0].scoring_inputs())
    print(f"warm-up (excluded from results): {warmup.mode} in {warmup.latency_ms} ms")

    rows = []
    for fixture in fixtures:
        truth = fixture.meta.ground_truth_service
        for run in range(1, args.runs + 1):
            result = pipeline.analyze_inputs(fixture.scoring_inputs())
            AnalyzeResponse.model_validate(result.response.model_dump())  # raises if not contract-valid
            top = result.response.hypotheses[0]
            diagnosis = result.llm.diagnosis if result.llm else None
            rows.append(
                {
                    "fixture": fixture.event.anomaly_id,
                    "mode": result.mode,
                    "attempts": result.llm.attempts if result.llm else 0,
                    "latency_ms": result.latency_ms,
                    "estimated_tokens": result.prompt.estimated_tokens if result.prompt else None,
                    "prompt_tokens": result.llm.prompt_tokens if result.llm else None,
                    "hypotheses": len(result.response.hypotheses),
                    "top_service": result.services[0],
                    "scorer_top": result.report.candidates[0].service,
                    "action": top.proposed_action,
                    "adjustments": diagnosis.adjustments if diagnosis else [],
                    "guardrail_rejected": len(result.guardrail_rejections),
                    "truth": truth,
                    "errors": result.llm.errors if result.llm else [result.fallback_reason],
                }
            )
            print(f"  {fixture.event.anomaly_id} run {run:2d}: {result.mode:<22} attempts={rows[-1]['attempts']} "
                  f"{result.latency_ms:6d} ms  hyps={rows[-1]['hypotheses']}  top={result.services[0]}  "
                  f"action={top.proposed_action}  adjustments={len(rows[-1]['adjustments'])}")

    print("\nPer fixture (truth = ground-truth root cause; 'kept scorer top' = rank-1 service equals the scorer's rank 1)")
    print(f"{'fixture':<11} {'truth':<10} {'runs':>4} {'1st-try':>7} {'retried':>7} {'fallback':>8} {'p50 ms':>7} "
          f"{'p95 ms':>7} {'rank-1=truth':>12} {'kept scorer top':>15} {'adjusted':>8}  most common rank-1 action")
    for fixture in fixtures:
        mine = [r for r in rows if r["fixture"] == fixture.event.anomaly_id]
        latencies = [r["latency_ms"] for r in mine]
        first = sum(r["mode"] == "llm" and r["attempts"] == 1 for r in mine)
        retried = sum(r["mode"] == "llm" and r["attempts"] > 1 for r in mine)
        fallback = sum(r["mode"] != "llm" for r in mine)
        truth = mine[0]["truth"]
        correct = f"{sum(r['top_service'] == truth for r in mine)}/{len(mine)}" if truth else "n/a"
        kept = f"{sum(r['top_service'] == r['scorer_top'] for r in mine)}/{len(mine)}"
        adjusted = sum(bool(r["adjustments"]) for r in mine)
        action, count = Counter(r["action"] for r in mine).most_common(1)[0]
        print(f"{fixture.event.anomaly_id:<11} {truth or '-':<10} {len(mine):>4} {first:>7} {retried:>7} {fallback:>8} "
              f"{percentile(latencies, 0.5):>7} {percentile(latencies, 0.95):>7} {correct:>12} {kept:>15} {adjusted:>8}  "
              f"{action} ({count}/{len(mine)})")

    latencies = [r["latency_ms"] for r in rows]
    retried = sum(r["mode"] == "llm" and r["attempts"] > 1 for r in rows)
    fallback = sum(r["mode"] != "llm" for r in rows)
    with_truth = [r for r in rows if r["truth"]]
    ratios = [r["prompt_tokens"] / r["estimated_tokens"] for r in rows if r["prompt_tokens"] and r["estimated_tokens"]]
    print("\nOverall")
    print(f"  runs                         {len(rows)}")
    print(f"  contract-valid responses     {len(rows)}/{len(rows)}")
    print(f"  valid on first attempt       {sum(r['mode'] == 'llm' and r['attempts'] == 1 for r in rows)}/{len(rows)}")
    print(f"  needed a retry               {retried}/{len(rows)} ({retried / len(rows):.0%})")
    print(f"  deterministic fallback       {fallback}/{len(rows)} ({fallback / len(rows):.0%})")
    print(f"  latency p50 / p95 / max      {percentile(latencies, 0.5)} / {percentile(latencies, 0.95)} / {max(latencies)} ms")
    print(f"  mean attempts                {statistics.mean(r['attempts'] for r in rows):.2f}")
    if with_truth:
        print(f"  rank-1 service is the true root cause  {sum(r['top_service'] == r['truth'] for r in with_truth)}/{len(with_truth)}")
        print(f"  scorer's rank-1 is the true root cause {sum(r['scorer_top'] == r['truth'] for r in with_truth)}/{len(with_truth)}")
    print(f"  replies adjusted (duplicate dropped or reordered)  {sum(bool(r['adjustments']) for r in rows)}/{len(rows)}")
    print(f"  hypotheses dropped by the evidence guardrail       {sum(r['guardrail_rejected'] for r in rows)}")
    print(f"  rank-1 actions                         {dict(Counter(r['action'].split(':')[0] for r in rows))}")
    if ratios:
        print(f"  actual/estimated prompt tokens       mean {statistics.mean(ratios):.2f} "
              f"(min {min(ratios):.2f}, max {max(ratios):.2f}; below 1 means the budget estimate is safe)")
    problems = Counter(error.split(": ", 1)[-1][:120] for r in rows for error in r["errors"] if error)
    if problems:
        print("  most common rejection/fallback reasons:")
        for problem, count in problems.most_common(5):
            print(f"    {count:3d}  {problem}")


if __name__ == "__main__":
    main()
