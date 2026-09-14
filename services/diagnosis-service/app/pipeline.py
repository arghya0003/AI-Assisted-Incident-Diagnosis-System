"""/analyze orchestration: scoring inputs -> retrieval -> deterministic scoring -> LLM -> evidence guardrail.

The deterministic ranking is returned instead of the LLM's answer when the prompt can't fit the
context budget, when the LLM gives no valid response (unreachable, or invalid on every attempt), or
when the guardrail rejects every LLM hypothesis. So /analyze always returns a contract-valid
response, and every response, LLM or deterministic, passes the guardrail before it leaves.
"""

import dataclasses
import logging
import time
from dataclasses import dataclass, field
from typing import Literal

from app.db import AnomalyStore
from app.deterministic import deterministic_diagnosis
from app.graph import DependencyGraph
from app.guardrail import GuardrailResult, GuardrailStats, Rejection, Source, allowed_ids, apply_guardrail
from app.hypotheses import Diagnosis
from app.llm import Chat, LLMDiagnoser, LLMOutcome
from app.models import AnalyzeResponse, CandidateReport
from app.ollama import OllamaClient
from app.prompts import Prompt, PromptTooLarge, build_prompt
from app.retrieval import Embed, Retriever
from app.scoring import ScoringConfig, ScoringInputs, score_candidates
from app.settings import Settings

log = logging.getLogger("diagnosis-service.pipeline")

DiagnosisMode = Literal["llm", "deterministic_fallback"]


@dataclass(frozen=True)
class PipelineConfig:
    context_tokens: int
    response_reserve_tokens: int
    max_prompt_candidates: int
    min_prompt_candidates: int
    retrieval_mode: str
    retrieval_top_k: int
    llm_max_attempts: int

    @classmethod
    def from_settings(cls, settings: Settings) -> "PipelineConfig":
        return cls(
            context_tokens=settings.llm_context_tokens,
            response_reserve_tokens=settings.llm_response_reserve_tokens,
            max_prompt_candidates=settings.prompt_max_candidates,
            min_prompt_candidates=settings.prompt_min_candidates,
            retrieval_mode=settings.retrieval_mode,
            retrieval_top_k=settings.retrieval_top_k,
            llm_max_attempts=settings.llm_max_attempts,
        )


@dataclass(frozen=True)
class PipelineResult:
    response: AnalyzeResponse
    services: list[str]  # the candidate each hypothesis is about, in rank order
    mode: DiagnosisMode
    report: CandidateReport
    prompt: Prompt | None  # None only when the prompt could not be built
    llm: LLMOutcome | None  # None when the LLM was not called
    fallback_reason: str | None
    latency_ms: int
    # Hypotheses the evidence guardrail dropped while producing this result.
    guardrail_rejections: list[Rejection] = field(default_factory=list)


def ollama_embed(client: OllamaClient, settings: Settings) -> Embed:
    return lambda texts: client.embed(texts, settings.embed_model)


def ollama_chat(client: OllamaClient, settings: Settings) -> Chat:
    options = {
        "num_ctx": settings.llm_context_tokens,
        "temperature": settings.llm_temperature,
        "num_predict": settings.llm_max_output_tokens,
    }
    return lambda messages, schema: client.chat(
        messages, settings.llm_model, schema, options, timeout_seconds=settings.llm_timeout_seconds
    )


class DiagnosisPipeline:
    def __init__(
        self,
        store: AnomalyStore,
        embed: Embed,
        chat: Chat,
        graph: DependencyGraph,
        scoring: ScoringConfig,
        config: PipelineConfig,
        stats: GuardrailStats | None = None,
    ):
        self._store = store
        self._embed = embed
        self._chat = chat
        self._graph = graph
        self._scoring = scoring
        self._config = config
        self._stats = stats

    def scoring_inputs(self, anomaly_id: str) -> ScoringInputs | None:
        return self._store.scoring_inputs(
            anomaly_id, self._scoring.co_anomaly_window_seconds, self._scoring.deploy_lookback_minutes
        )

    def candidate_report(self, anomaly_id: str) -> CandidateReport | None:
        inputs = self.scoring_inputs(anomaly_id)
        return None if inputs is None else self.report_for(inputs)

    def report_for(self, inputs: ScoringInputs) -> CandidateReport:
        retriever = Retriever(
            self._embed, self._store, self._graph, mode=self._config.retrieval_mode, top_k=self._config.retrieval_top_k
        )
        retrieval = retriever.retrieve(inputs.anomaly)
        inputs = dataclasses.replace(inputs, similar_incidents=retrieval.incidents, retrieval_status=retrieval.status)
        return score_candidates(inputs, self._graph, self._scoring)

    def analyze(self, anomaly_id: str) -> PipelineResult | None:
        inputs = self.scoring_inputs(anomaly_id)
        return None if inputs is None else self.analyze_inputs(inputs)

    def analyze_inputs(self, inputs: ScoringInputs) -> PipelineResult:
        started = time.perf_counter()
        report = self.report_for(inputs)
        try:
            prompt = build_prompt(
                inputs.anomaly,
                report,
                context_tokens=self._config.context_tokens,
                response_reserve_tokens=self._config.response_reserve_tokens,
                max_candidates=self._config.max_prompt_candidates,
                min_candidates=self._config.min_prompt_candidates,
            )
        except PromptTooLarge as exc:
            return self._fallback(report, None, None, str(exc), started, [])

        outcome = LLMDiagnoser(self._chat, self._config.llm_max_attempts).diagnose(prompt)
        if outcome.diagnosis is None:
            last_error = outcome.errors[-1] if outcome.errors else "no error recorded"
            reason = f"no valid LLM response after {outcome.attempts} attempt(s); last: {last_error}"
            return self._fallback(report, prompt, outcome, reason, started, [])

        guarded = self._guard(outcome.diagnosis, report, prompt, "llm")
        if not guarded.diagnosis.services:
            reason = f"the evidence guardrail rejected all {guarded.checked} LLM hypotheses"
            return self._fallback(report, prompt, outcome, reason, started, guarded.rejections)
        return PipelineResult(
            response=guarded.diagnosis.response,
            services=guarded.diagnosis.services,
            mode="llm",
            report=report,
            prompt=prompt,
            llm=outcome,
            fallback_reason=None,
            latency_ms=_elapsed_ms(started),
            guardrail_rejections=guarded.rejections,
        )

    def _guard(self, diagnosis: Diagnosis, report: CandidateReport, prompt: Prompt | None, source: Source) -> GuardrailResult:
        result = apply_guardrail(diagnosis, allowed_ids(report, prompt))
        if self._stats is not None:
            self._stats.record(result, source)
        return result

    def _fallback(
        self,
        report: CandidateReport,
        prompt: Prompt | None,
        outcome: LLMOutcome | None,
        reason: str,
        started: float,
        earlier_rejections: list[Rejection],
    ) -> PipelineResult:
        log.warning("anomaly %s: returning the deterministic ranking: %s", report.anomaly_id, reason)
        guarded = self._guard(deterministic_diagnosis(report), report, prompt, "deterministic")
        if guarded.rejections:
            log.error("the deterministic ranking for %s failed the evidence guardrail; this is a bug", report.anomaly_id)
        return PipelineResult(
            response=guarded.diagnosis.response,
            services=guarded.diagnosis.services,
            mode="deterministic_fallback",
            report=report,
            prompt=prompt,
            llm=outcome,
            fallback_reason=reason,
            latency_ms=_elapsed_ms(started),
            guardrail_rejections=[*earlier_rejections, *guarded.rejections],
        )


def _elapsed_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)
