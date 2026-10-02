"""/analyze orchestration, in one of four pipeline modes (PLAN.md, Phase 8):

- full: scoring inputs -> retrieval -> deterministic scoring -> LLM -> evidence guardrail
- no_graph: the same, with the graph-proximity weight zeroed and the other weights rescaled
- deterministic: scoring and retrieval only; the deterministic ranking is the answer, with no LLM
- llm_only: the anomaly, related anomalies and raw deploys straight to the LLM, with no scoring,
  retrieval or graph; then the guardrail

In full and no_graph, the deterministic ranking replaces the LLM's answer when the prompt can't fit
the context budget, the LLM gives no valid response, or the guardrail rejects every LLM hypothesis,
so those modes always answer. llm_only has no fallback, so the ablation measures the LLM alone: a
failure there is an empty answer. Every hypothesis passes the guardrail before it leaves.
"""

import dataclasses
import logging
import time
from dataclasses import dataclass, field

from app.db import AnomalyStore
from app.deterministic import deterministic_diagnosis
from app.graph import DependencyGraph
from app.guardrail import GuardrailResult, GuardrailStats, Rejection, Source, allowed_ids, apply_guardrail
from app.hypotheses import Diagnosis, rollback_window_minutes
from app.llm import Chat, LLMDiagnoser, LLMOutcome
from app.models import AnalyzeResponse, AnsweredBy, CandidateReport, PipelineMode
from app.ollama import OllamaClient
from app.openrouter import OpenRouterClient
from app.providers import ChatReply, ProviderUnavailable
from app.prompts import Prompt, PromptTooLarge, build_llm_only_prompt, build_prompt
from app.retrieval import Embed, Retriever
from app.scoring import ScoringConfig, ScoringInputs, score_candidates
from app.settings import Settings

log = logging.getLogger("diagnosis-service.pipeline")


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
    mode: AnsweredBy  # how the answer was produced
    pipeline_mode: PipelineMode
    report: CandidateReport | None  # None in llm_only mode, which does no scoring
    prompt: Prompt | None  # None when no LLM prompt was built
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


# Providers that speak the OpenAI chat shape, and therefore share one client.
HTTP_PROVIDERS = ("gemini", "openrouter")
# How each spells the thinking budget. Gemini rejects OpenRouter's `reasoning` object outright, so
# this is not cosmetic: get it wrong and every request to that provider fails with HTTP 400.
REASONING_STYLE = {"gemini": "openai", "openrouter": "openrouter"}


def chat_chain(settings: Settings) -> list[tuple[str, str]]:
    """The (provider, model) pairs to try, in order, de-duplicated.

    A fallback entry may name its provider as "provider:model" so the chain can cross vendors. That
    is the point of it: Gemini returns transient 503s under load while OpenRouter exhausts a daily
    quota, and those two failures are unlikely to coincide. An entry with no provider uses the
    primary's, which keeps the common case short.
    """
    chain = [(settings.llm_provider, settings.llm_model)]
    for entry in settings.llm_fallback_models:
        provider, separator, model = entry.partition(":")
        # A bare model name can itself contain a colon (OpenRouter's ":free" suffix), so only treat
        # the prefix as a provider when it actually names one.
        if separator and provider in HTTP_PROVIDERS:
            pair = (provider, model)
        else:
            pair = (settings.llm_provider, entry)
        if pair not in chain:
            chain.append(pair)
    return chain


def http_client_for(provider: str, settings: Settings) -> OpenRouterClient:
    url, key = {
        "gemini": (settings.gemini_url, settings.gemini_api_key),
        "openrouter": (settings.openrouter_url, settings.openrouter_api_key),
    }[provider]
    return OpenRouterClient(key, url, timeout_seconds=settings.llm_timeout_seconds)


def api_chat(settings: Settings, clients: dict[str, OpenRouterClient] | None = None) -> Chat:
    """Generation over HTTP, trying each (provider, model) in the chain until one answers.

    Only availability failures move on: a rate limit, a transient overload, a timeout. A reply that
    parses but breaks the contract is the model's own behaviour and is retried against the *same*
    model by app/llm.py, so every stored answer stays attributable to the model that wrote it. That
    model is recorded on the reply, which is what `model_version` and X-Model-Version report.
    """
    chain = chat_chain(settings)
    clients = clients or {provider: http_client_for(provider, settings) for provider, _ in set(chain)}

    def chat(messages: list[dict[str, str]], schema: dict) -> ChatReply:
        failures = []
        for position, (provider, model) in enumerate(chain, start=1):
            try:
                reply = clients[provider].chat(
                    messages,
                    model,
                    schema,
                    temperature=settings.llm_temperature,
                    max_output_tokens=settings.llm_max_output_tokens,
                    reasoning_effort=settings.llm_reasoning_effort or None,
                    reasoning_style=REASONING_STYLE[provider],
                    timeout_seconds=settings.llm_timeout_seconds,
                )
            except ProviderUnavailable as exc:
                failures.append(f"{provider}/{model}: {exc}")
                if position < len(chain):
                    log.warning("%s/%s unavailable (%s); trying %s/%s", provider, model, exc, *chain[position])
                continue
            if position > 1:
                log.info("answered by fallback %s/%s after %d unavailable", provider, model, position - 1)
            return reply
        raise ProviderUnavailable("; ".join(failures))

    return chat


def build_chat(settings: Settings, ollama: OllamaClient, clients: dict[str, OpenRouterClient] | None = None) -> Chat:
    """The configured generation provider. Ollama stays selectable so the recorded phi4-mini
    results can be reproduced, and so the system can be demonstrated without an API key. It is a
    primary only: its request shape differs, so it is not part of the HTTP fallback chain."""
    if settings.llm_provider == "ollama":
        return ollama_chat(ollama, settings)
    return api_chat(settings, clients)


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

    def report_for(self, inputs: ScoringInputs, scoring: ScoringConfig | None = None) -> CandidateReport:
        retriever = Retriever(
            self._embed, self._store, self._graph, mode=self._config.retrieval_mode, top_k=self._config.retrieval_top_k
        )
        retrieval = retriever.retrieve(inputs.anomaly)
        inputs = dataclasses.replace(inputs, similar_incidents=retrieval.incidents, retrieval_status=retrieval.status)
        return score_candidates(inputs, self._graph, scoring or self._scoring)

    def analyze(self, anomaly_id: str, mode: PipelineMode = "full") -> PipelineResult | None:
        inputs = self.scoring_inputs(anomaly_id)
        return None if inputs is None else self.analyze_inputs(inputs, mode)

    def analyze_inputs(self, inputs: ScoringInputs, mode: PipelineMode = "full") -> PipelineResult:
        started = time.perf_counter()
        if mode == "llm_only":
            return self._llm_only(inputs, started)

        scoring = self._scoring.without_graph() if mode == "no_graph" else self._scoring
        report = self.report_for(inputs, scoring)
        if mode == "deterministic":
            return self._deterministic(report, None, None, None, started, [], mode, "deterministic")

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
            return self._deterministic(report, None, None, str(exc), started, [], mode, "deterministic_fallback")

        outcome = LLMDiagnoser(self._chat, self._config.llm_max_attempts).diagnose(prompt)
        if outcome.diagnosis is None:
            reason = _no_valid_reply(outcome)
            return self._deterministic(report, prompt, outcome, reason, started, [], mode, "deterministic_fallback")

        guarded = self._guard(outcome.diagnosis, report.anomaly_id, report, prompt, "llm")
        if not guarded.diagnosis.services:
            reason = f"the evidence guardrail rejected all {guarded.checked} LLM hypotheses"
            return self._deterministic(
                report, prompt, outcome, reason, started, guarded.rejections, mode, "deterministic_fallback"
            )
        return PipelineResult(
            response=guarded.diagnosis.response,
            services=guarded.diagnosis.services,
            mode="llm",
            pipeline_mode=mode,
            report=report,
            prompt=prompt,
            llm=outcome,
            fallback_reason=None,
            latency_ms=_elapsed_ms(started),
            guardrail_rejections=guarded.rejections,
        )

    def _llm_only(self, inputs: ScoringInputs, started: float) -> PipelineResult:
        anomaly = inputs.anomaly
        try:
            prompt = build_llm_only_prompt(
                anomaly,
                inputs.related,
                inputs.deploys,
                sorted(self._graph.nodes | set(anomaly.services)),
                window_seconds=self._scoring.co_anomaly_window_seconds,
                lookback_minutes=self._scoring.deploy_lookback_minutes,
                rollback_max_minutes=rollback_window_minutes(self._scoring.deploy_decay_minutes),
                context_tokens=self._config.context_tokens,
                response_reserve_tokens=self._config.response_reserve_tokens,
            )
        except PromptTooLarge as exc:
            return self._no_answer(anomaly.anomaly_id, None, None, str(exc), started, [])

        outcome = LLMDiagnoser(self._chat, self._config.llm_max_attempts).diagnose(prompt)
        if outcome.diagnosis is None:
            return self._no_answer(anomaly.anomaly_id, prompt, outcome, _no_valid_reply(outcome), started, [])

        guarded = self._guard(outcome.diagnosis, anomaly.anomaly_id, None, prompt, "llm")
        if not guarded.diagnosis.services:
            reason = f"the evidence guardrail rejected all {guarded.checked} LLM hypotheses"
            return self._no_answer(anomaly.anomaly_id, prompt, outcome, reason, started, guarded.rejections)
        return PipelineResult(
            response=guarded.diagnosis.response,
            services=guarded.diagnosis.services,
            mode="llm",
            pipeline_mode="llm_only",
            report=None,
            prompt=prompt,
            llm=outcome,
            fallback_reason=None,
            latency_ms=_elapsed_ms(started),
            guardrail_rejections=guarded.rejections,
        )

    def _guard(
        self,
        diagnosis: Diagnosis,
        anomaly_id: str,
        report: CandidateReport | None,
        prompt: Prompt | None,
        source: Source,
    ) -> GuardrailResult:
        result = apply_guardrail(diagnosis, allowed_ids(anomaly_id, report, prompt))
        if self._stats is not None:
            self._stats.record(result, source)
        return result

    def _deterministic(
        self,
        report: CandidateReport,
        prompt: Prompt | None,
        outcome: LLMOutcome | None,
        reason: str | None,
        started: float,
        earlier_rejections: list[Rejection],
        pipeline_mode: PipelineMode,
        answered_by: AnsweredBy,
    ) -> PipelineResult:
        if reason:
            log.warning("anomaly %s: returning the deterministic ranking: %s", report.anomaly_id, reason)
        guarded = self._guard(deterministic_diagnosis(report), report.anomaly_id, report, prompt, "deterministic")
        if guarded.rejections:
            log.error("the deterministic ranking for %s failed the evidence guardrail; this is a bug", report.anomaly_id)
        return PipelineResult(
            response=guarded.diagnosis.response,
            services=guarded.diagnosis.services,
            mode=answered_by,
            pipeline_mode=pipeline_mode,
            report=report,
            prompt=prompt,
            llm=outcome,
            fallback_reason=reason,
            latency_ms=_elapsed_ms(started),
            guardrail_rejections=[*earlier_rejections, *guarded.rejections],
        )

    def _no_answer(
        self,
        anomaly_id: str,
        prompt: Prompt | None,
        outcome: LLMOutcome | None,
        reason: str,
        started: float,
        rejections: list[Rejection],
    ) -> PipelineResult:
        log.warning("anomaly %s (llm_only): no answer, and this mode has no fallback: %s", anomaly_id, reason)
        return PipelineResult(
            response=AnalyzeResponse(hypotheses=[]),
            services=[],
            mode="llm_failed",
            pipeline_mode="llm_only",
            report=None,
            prompt=prompt,
            llm=outcome,
            fallback_reason=reason,
            latency_ms=_elapsed_ms(started),
            guardrail_rejections=rejections,
        )


def _no_valid_reply(outcome: LLMOutcome) -> str:
    last_error = outcome.errors[-1] if outcome.errors else "no error recorded"
    return f"no valid LLM response after {outcome.attempts} attempt(s); last: {last_error}"


def _elapsed_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)
