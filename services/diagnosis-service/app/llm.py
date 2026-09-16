"""phi4-mini call with validate-and-retry.

Each reply is parsed and checked against the prompt's candidates: every hypothesis must name a
listed candidate and propose one of that candidate's actions, and its cause may not talk about a
deploy unless that candidate has one listed. A rejected reply is sent back with
the reasons, so the next attempt can correct it. After the last attempt, or if Ollama is
unreachable, the outcome carries no diagnosis and the pipeline falls back to the deterministic
ranking. Evidence citations are checked separately by the guardrail (Phase 7), not here.

A reply that passes is normalised rather than retried for two cosmetic problems seen in practice:
a second hypothesis about the same candidate is dropped, and hypotheses are reordered so rank
follows confidence. Both are recorded as adjustments, so evaluation can count them.
"""

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.hypotheses import MAX_HYPOTHESES, Diagnosis
from app.models import AnalyzeResponse, Hypothesis, NonBlankId
from app.ollama import ChatReply, OllamaUnavailable
from app.prompts import Prompt, retry_message

log = logging.getLogger("diagnosis-service.llm")

# (messages, JSON schema) -> reply
Chat = Callable[[list[dict[str, str]], dict], ChatReply]

_DEPLOY_WORDS = r"(?:re)?deploy\w*|releases?|released|rollouts?|roll(?:ed|s|ing)? out|upgrade\w*"
_DEPLOY_CLAIM = re.compile(rf"\b(?:{_DEPLOY_WORDS})\b", re.IGNORECASE)
# "no recent deploy", "not a deploy", "without any release": saying there was none is fine.
_NEGATED_DEPLOY = re.compile(rf"\b(?:no|not|without|never|nor)\s+(?:\w+\s+){{0,3}}?(?:{_DEPLOY_WORDS})\b", re.IGNORECASE)


def claims_a_deploy(cause: str) -> bool:
    """Whether a cause asserts that a deploy, release, rollout or upgrade happened. A cheap check
    for the most harmful invented fact: phi4-mini copied deploy stories from similar past incidents
    into causes for candidates that had no deploy at all."""
    return bool(_DEPLOY_CLAIM.search(_NEGATED_DEPLOY.sub(" ", cause)))


class LLMHypothesis(BaseModel):
    """A hypothesis as the LLM returns it: the contract fields plus the candidate it is about."""

    model_config = ConfigDict(extra="forbid")

    rank: int = Field(ge=1)
    service: str
    cause: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_ids: list[NonBlankId] = Field(min_length=1)
    proposed_action: str


class LLMReply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hypotheses: list[LLMHypothesis]


@dataclass(frozen=True)
class LLMOutcome:
    diagnosis: Diagnosis | None
    attempts: int
    errors: list[str] = field(default_factory=list)  # one entry per rejected or failed attempt
    latency_ms: int = 0
    prompt_tokens: int | None = None


def validate_reply(content: str, prompt: Prompt) -> tuple[Diagnosis | None, list[str]]:
    try:
        reply = LLMReply.model_validate_json(content)
    except ValidationError as exc:
        return None, [_describe(error) for error in exc.errors()[:5]]

    options = {option.service: option for option in prompt.options}
    supplied = set(prompt.citable_ids)
    errors = []
    if not reply.hypotheses:
        errors.append("return at least one hypothesis")
    if len(reply.hypotheses) > MAX_HYPOTHESES:
        errors.append(f"return at most {MAX_HYPOTHESES} hypotheses, not {len(reply.hypotheses)}")
    for hypothesis in reply.hypotheses:
        option = options.get(hypothesis.service)
        if option is None:
            errors.append(f"hypothesis {hypothesis.rank}: service {hypothesis.service!r} is not a listed candidate")
        elif hypothesis.proposed_action not in option.actions:
            errors.append(
                f"hypothesis {hypothesis.rank}: proposed_action {hypothesis.proposed_action!r} is not in "
                f"{hypothesis.service}'s may-propose list"
            )
        elif not option.has_recent_deploy and claims_a_deploy(hypothesis.cause):
            errors.append(
                f"hypothesis {hypothesis.rank}: the cause mentions a deploy or release, but {hypothesis.service} "
                "has no recent deploy listed; state only facts listed under that candidate"
            )
        else:
            # Misattributed evidence: a real id that belongs to a DIFFERENT listed candidate. The
            # response schema binds each candidate to its own ids while Ollama decodes, and the
            # evidence guardrail drops ids that were never supplied at all - but the guardrail
            # accepts the union of every candidate's ids, so a hypothesis about A citing B's real
            # deploy id passes both. It is caught here, where the per-candidate lists still exist.
            #
            # Deliberately limited to ids that appear somewhere in the prompt. An id that was never
            # supplied (a fabricated `ev-9999`) is left to the guardrail, which drops that single
            # hypothesis and keeps the rest; failing validation instead would spend all three
            # attempts and fall back, losing the clean hypotheses with it.
            misattributed = [i for i in hypothesis.evidence_ids if i in supplied and i not in option.citable_ids]
            if misattributed:
                errors.append(
                    f"hypothesis {hypothesis.rank}: {', '.join(misattributed)} is listed under another candidate, "
                    f"not under {hypothesis.service}; cite only evidence listed under that candidate"
                )
    if errors:
        return None, errors
    return _normalise(reply.hypotheses), []


def _normalise(hypotheses: list[LLMHypothesis]) -> Diagnosis:
    adjustments = []
    kept, seen = [], set()
    for hypothesis in sorted(hypotheses, key=lambda h: h.rank):
        if hypothesis.service in seen:
            adjustments.append(f"dropped a second hypothesis about {hypothesis.service}")
            continue
        seen.add(hypothesis.service)
        kept.append(hypothesis)
    ordered = sorted(kept, key=lambda h: -h.confidence)  # stable: ties keep the model's order
    if [h.service for h in ordered] != [h.service for h in kept]:
        adjustments.append("reordered hypotheses so rank follows confidence")
    response = AnalyzeResponse(
        hypotheses=[
            Hypothesis(
                rank=rank,
                cause=h.cause,
                confidence=h.confidence,
                evidence_ids=h.evidence_ids,
                proposed_action=h.proposed_action,
            )
            for rank, h in enumerate(ordered, start=1)
        ]
    )
    return Diagnosis(response=response, services=[h.service for h in ordered], adjustments=adjustments)


class LLMDiagnoser:
    def __init__(self, chat: Chat, max_attempts: int = 3):
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
        self._chat = chat
        self._max_attempts = max_attempts

    def diagnose(self, prompt: Prompt) -> LLMOutcome:
        started = time.perf_counter()
        messages = list(prompt.messages)
        errors: list[str] = []
        prompt_tokens = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                reply = self._chat(messages, prompt.schema)
            except OllamaUnavailable as exc:
                # The client has already retried transport and 5xx errors; don't multiply them.
                errors.append(f"attempt {attempt}: ollama unavailable: {exc}")
                log.warning("llm attempt %d: ollama unavailable: %s", attempt, exc)
                return LLMOutcome(None, attempt, errors, _elapsed_ms(started), prompt_tokens)
            prompt_tokens = reply.prompt_tokens
            diagnosis, problems = validate_reply(reply.content, prompt)
            if diagnosis is None and reply.done_reason == "length":
                problems = [
                    "the response hit the output token limit before it was complete; keep it short and "
                    "list each evidence id once",
                    *problems[:2],
                ]
            if diagnosis is not None:
                if attempt > 1:
                    log.info("llm reply accepted on attempt %d", attempt)
                if diagnosis.adjustments:
                    log.info("llm reply adjusted: %s", "; ".join(diagnosis.adjustments))
                return LLMOutcome(diagnosis, attempt, errors, _elapsed_ms(started), prompt_tokens)
            errors.append(f"attempt {attempt}: " + "; ".join(problems))
            log.warning("llm attempt %d/%d rejected: %s", attempt, self._max_attempts, "; ".join(problems))
            messages = [
                *messages,
                {"role": "assistant", "content": reply.content},
                {"role": "user", "content": retry_message(problems)},
            ]
        return LLMOutcome(None, self._max_attempts, errors, _elapsed_ms(started), prompt_tokens)


def _describe(error: dict) -> str:
    location = ".".join(str(part) for part in error.get("loc", ())) or "response"
    return f"{location}: {error.get('msg', 'invalid')}"


def _elapsed_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)
