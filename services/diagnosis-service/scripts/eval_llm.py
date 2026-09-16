"""Run fixtures through the real pipeline (retrieval, scoring and phi4-mini) in one or more pipeline
modes, and report validity, retries, fallbacks, latency and accuracy for each mode.

Phase 6 used this for validate-and-retry rates. Phase 8 uses it for the ablation: the same fixtures
in the full, llm_only, no_graph and deterministic modes. Each fixture's scenario context (deploys and
related anomalies) is used, so every mode sees what that scenario would have produced. With
--persist, every run is also stored in the analyses and hypotheses tables for comparison in SQL.

Run inside the compose network, with Ollama on the host:
    EVAL_RUNS=10 bash services/diagnosis-service/scripts/test_in_docker.sh --eval
    EVAL_RUNS=1 EVAL_ARGS="--modes full,llm_only,no_graph,deterministic --persist" \
        bash services/diagnosis-service/scripts/test_in_docker.sh --eval
"""

import argparse
import math
import os
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import get_args

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("PG_HOST", "localhost")
os.environ.setdefault("OLLAMA_URL", "http://localhost:11434")

from app.analyses import config_fingerprint, model_version_for, new_analysis_id, stored_analysis  # noqa: E402
from app.db import PostgresAnomalyStore  # noqa: E402
from app.fixtures import load_fixtures  # noqa: E402
from app.graph import load_graph  # noqa: E402
from app.main import SERVICE_VERSION  # noqa: E402
from app.models import AnalyzeResponse, PipelineMode  # noqa: E402
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
    parser.add_argument("--runs", type=int, default=10, help="runs per fixture per mode")
    parser.add_argument("--modes", default="full", help="comma-separated: full, llm_only, no_graph, deterministic")
    parser.add_argument("--persist", action="store_true", help="store every run in the analyses and hypotheses tables")
    args = parser.parse_args()
    modes = [mode.strip() for mode in args.modes.split(",") if mode.strip()]
    unknown = set(modes) - set(get_args(PipelineMode))
    if unknown:
        parser.error(f"unknown mode(s) {sorted(unknown)}; expected {get_args(PipelineMode)}")

    settings = Settings.from_env()
    client = OllamaClient(settings.ollama_url, timeout_seconds=settings.ollama_timeout_seconds)
    store = PostgresAnomalyStore(settings)
    scoring = ScoringConfig.from_settings(settings)
    pipeline_config = PipelineConfig.from_settings(settings)
    pipeline = DiagnosisPipeline(
        store, ollama_embed(client, settings), ollama_chat(client, settings), load_graph(), scoring, pipeline_config
    )
    fingerprint = config_fingerprint(SERVICE_VERSION, settings, scoring, pipeline_config)
    fixtures = [f for f in load_fixtures() if not args.anomaly_ids or f.event.anomaly_id in args.anomaly_ids]
    print(f"model={settings.llm_model} num_ctx={settings.llm_context_tokens} temperature={settings.llm_temperature} "
          f"attempts={settings.llm_max_attempts} runs={args.runs} fixtures={len(fixtures)} modes={modes} "
          f"persist={args.persist} fingerprint={fingerprint}")

    llm_modes = [mode for mode in modes if mode != "deterministic"]
    if llm_modes:
        warmup = pipeline.analyze_inputs(fixtures[0].scoring_inputs(), llm_modes[0])
        print(f"warm-up (excluded from results): {warmup.mode} in {warmup.latency_ms} ms")

    rows = []
    for fixture in fixtures:
        anomaly_id = fixture.event.anomaly_id
        truth = fixture.meta.ground_truth_service
        for mode in modes:
            for run in range(1, args.runs + 1):
                result = pipeline.analyze_inputs(fixture.scoring_inputs(), mode)
                AnalyzeResponse.model_validate(result.response.model_dump())  # raises if not contract-valid
                if args.persist:
                    record = stored_analysis(result, new_analysis_id(), anomaly_id, model_version_for(mode, settings), fingerprint)
                    store.save_analysis(record, result.report.evidence if result.report else [])
                top = result.response.hypotheses[0] if result.response.hypotheses else None
                diagnosis = result.llm.diagnosis if result.llm else None
                rows.append(
                    {
                        "fixture": anomaly_id,
                        "mode": mode,
                        "answered_by": result.mode,
                        "attempts": result.llm.attempts if result.llm else 0,
                        "latency_ms": result.latency_ms,
                        "estimated_tokens": result.prompt.estimated_tokens if result.prompt else None,
                        "prompt_tokens": result.llm.prompt_tokens if result.llm else None,
                        "top_service": result.services[0] if result.services else None,
                        "action": top.proposed_action if top else "(no answer)",
                        "adjusted": bool(diagnosis and diagnosis.adjustments),
                        "guardrail_rejected": len(result.guardrail_rejections),
                        "truth": truth,
                        "errors": (result.llm.errors if result.llm else []) + ([result.fallback_reason] if result.fallback_reason else []),
                    }
                )
                print(f"  {anomaly_id} {mode:<13} run {run:2d}: {result.mode:<22} attempts={rows[-1]['attempts']} "
                      f"{result.latency_ms:6d} ms  top={rows[-1]['top_service']}  action={rows[-1]['action']}")

    print("\nPer fixture and mode")
    print(f"{'fixture':<11} {'truth':<10} {'mode':<13} {'runs':>4} {'llm':>4} {'determ.':>7} {'fallbk':>6} {'failed':>6} "
          f"{'retried':>7} {'p50 ms':>7} {'rank-1=truth':>12}  most common rank-1 action")
    for fixture in fixtures:
        for mode in modes:
            mine = [r for r in rows if r["fixture"] == fixture.event.anomaly_id and r["mode"] == mode]
            answered = Counter(r["answered_by"] for r in mine)
            truth = mine[0]["truth"]
            correct = f"{sum(r['top_service'] == truth for r in mine)}/{len(mine)}" if truth else "n/a"
            action, count = Counter(r["action"] for r in mine).most_common(1)[0]
            retried = sum(r["attempts"] > 1 for r in mine)
            print(f"{fixture.event.anomaly_id:<11} {truth or '-':<10} {mode:<13} {len(mine):>4} {answered['llm']:>4} "
                  f"{answered['deterministic']:>7} {answered['deterministic_fallback']:>6} {answered['llm_failed']:>6} "
                  f"{retried:>7} {percentile([r['latency_ms'] for r in mine], 0.5):>7} {correct:>12}  {action} ({count}/{len(mine)})")

    for mode in modes:
        mine = [r for r in rows if r["mode"] == mode]
        with_truth = [r for r in mine if r["truth"]]
        latencies = [r["latency_ms"] for r in mine]
        answered = Counter(r["answered_by"] for r in mine)
        ratios = [r["prompt_tokens"] / r["estimated_tokens"] for r in mine if r["prompt_tokens"] and r["estimated_tokens"]]
        print(f"\nMode {mode}")
        print(f"  runs                                   {len(mine)}")
        print(f"  answered by                            {dict(answered)}")
        print(f"  needed a retry                         {sum(r['attempts'] > 1 for r in mine)}/{len(mine)}")
        print(f"  latency p50 / p95 / max                {percentile(latencies, 0.5)} / {percentile(latencies, 0.95)} / {max(latencies)} ms")
        if with_truth:
            print(f"  rank-1 service is the true root cause  {sum(r['top_service'] == r['truth'] for r in with_truth)}/{len(with_truth)}")
        print(f"  rank-1 actions                         {dict(Counter(r['action'].split(':')[0] for r in mine))}")
        print(f"  replies adjusted                       {sum(r['adjusted'] for r in mine)}/{len(mine)}")
        print(f"  hypotheses dropped by the guardrail    {sum(r['guardrail_rejected'] for r in mine)}")
        if ratios:
            print(f"  actual/estimated prompt tokens         mean {statistics.mean(ratios):.2f} (min {min(ratios):.2f}, max {max(ratios):.2f})")
        problems = Counter(error.split(": ", 1)[-1][:110] for r in mine for error in r["errors"] if error)
        for problem, count in problems.most_common(3):
            print(f"    {count:3d}  {problem}")


if __name__ == "__main__":
    main()
