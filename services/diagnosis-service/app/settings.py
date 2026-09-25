"""Runtime configuration from environment variables.

Names and defaults match the rest of docker-compose.yml (PG_*, KAFKA_BOOTSTRAP), so this
service is configured the same way as metrics-sink and deploy-emitter.
"""

import os
from dataclasses import dataclass

OPENROUTER_DEFAULT_URL = "https://openrouter.ai/api/v1"
# Free, 262k context, supports strict json_schema and a seed for reproducible runs. Verified
# against the real prompt before being made the default.
OPENROUTER_DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


def _list_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _default_model() -> str:
    """The default model depends on the provider, so LLM_MODEL rarely needs setting by hand."""
    provider = os.environ.get("LLM_PROVIDER") or "openrouter"
    return OPENROUTER_DEFAULT_MODEL if provider == "openrouter" else "phi4-mini"


def _choice_env(name: str, default: str, choices: tuple[str, ...]) -> str:
    value = os.environ.get(name) or default
    if value not in choices:
        raise ValueError(f"{name} must be one of {choices}, got {value!r}")
    return value


@dataclass(frozen=True)
class Settings:
    pg_host: str
    pg_port: int
    pg_db: str
    pg_user: str
    pg_password: str
    ollama_url: str
    llm_provider: str
    openrouter_url: str
    openrouter_api_key: str
    llm_fallback_models: tuple[str, ...]
    llm_reasoning_effort: str
    llm_model: str
    embed_model: str
    llm_context_tokens: int
    score_weight_deploy: float
    score_weight_graph: float
    score_weight_co_anomaly: float
    score_weight_incident: float
    deploy_lookback_minutes: float
    deploy_decay_minutes: float
    co_anomaly_window_seconds: float
    ollama_timeout_seconds: float
    retrieval_mode: str
    retrieval_top_k: int
    llm_temperature: float
    llm_timeout_seconds: float
    llm_max_attempts: int
    llm_response_reserve_tokens: int
    llm_max_output_tokens: int
    prompt_max_candidates: int
    prompt_min_candidates: int
    pipeline_mode: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            pg_host=os.environ.get("PG_HOST", "timescaledb"),
            pg_port=_int_env("PG_PORT", 5432),
            pg_db=os.environ.get("PG_DB", "metrics"),
            pg_user=os.environ.get("PG_USER", "postgres"),
            pg_password=os.environ.get("PG_PASSWORD", "Abcd1234#"),
            # Ollama runs on the host (it needs the GPU), not in a container. Still used for
            # embeddings, and selectable for generation so the phi4-mini results stay reproducible.
            ollama_url=os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434"),
            # OpenRouter is the default: the deployed system is online anyway, and no other machine
            # on the team has Ollama, so every integration run used to answer deterministic_fallback.
            llm_provider=_choice_env("LLM_PROVIDER", "openrouter", ("openrouter", "ollama")),
            openrouter_url=os.environ.get("OPENROUTER_URL", OPENROUTER_DEFAULT_URL),
            # From the gitignored .env at the repo root, passed through docker-compose.yml.
            openrouter_api_key=os.environ.get("OPENROUTER_API_KEY", ""),
            # Tried in order when a model is unreachable (free endpoints rate-limit often). Never
            # used to paper over a model writing an invalid answer - that is retried on the same
            # model, so every result stays attributable.
            llm_fallback_models=_list_env("LLM_FALLBACK_MODELS", ("qwen/qwen3.8-27b:free",)),
            # nemotron spent 569 of 719 output tokens reasoning on a single hypothesis; "low" keeps
            # the budget for the answer. Empty disables the parameter for models without reasoning.
            llm_reasoning_effort=_choice_env("LLM_REASONING_EFFORT", "low", ("", "low", "medium", "high")),
            llm_model=os.environ.get("LLM_MODEL", _default_model()),
            embed_model=os.environ.get("EMBED_MODEL", "nomic-embed-text"),
            # Phase 0: 8192 keeps prompt room with no measured latency cost over 4096.
            llm_context_tokens=_int_env("LLM_CONTEXT_TOKENS", 8192),
            # Candidate scoring (app/scoring.py). Weights must sum to 1; these are PLAN.md's
            # starting values, to be tuned against evaluation results in Week 8.
            score_weight_deploy=_float_env("SCORE_WEIGHT_DEPLOY", 0.40),
            score_weight_graph=_float_env("SCORE_WEIGHT_GRAPH", 0.25),
            score_weight_co_anomaly=_float_env("SCORE_WEIGHT_CO_ANOMALY", 0.20),
            score_weight_incident=_float_env("SCORE_WEIGHT_INCIDENT", 0.15),
            deploy_lookback_minutes=_float_env("DEPLOY_LOOKBACK_MINUTES", 30.0),
            deploy_decay_minutes=_float_env("DEPLOY_DECAY_MINUTES", 10.0),
            # Anomalies with onsets this close are treated as one incident. Derived from t_onset
            # because M2's evidence_window is zero-width (issue #3).
            co_anomaly_window_seconds=_float_env("CO_ANOMALY_WINDOW_SECONDS", 120.0),
            # Phase 0: a cold nomic-embed-text load took 27.5 s, so 60 s leaves room.
            ollama_timeout_seconds=_float_env("OLLAMA_TIMEOUT_SECONDS", 60.0),
            # Past-incident retrieval (app/retrieval.py). "vector" skips the structured pre-filter.
            retrieval_mode=_choice_env("RETRIEVAL_MODE", "hybrid", ("hybrid", "vector")),
            # Capped because retrieved incidents go into the 8K-token LLM prompt in Phase 6.
            retrieval_top_k=_int_env("RETRIEVAL_TOP_K", 3),
            # Low so the ranking is stable, which reproducible evaluation needs.
            llm_temperature=_float_env("LLM_TEMPERATURE", 0.1),
            # Per generation call. Phase 0 measured ~5 s warm; a cold model load adds ~30 s.
            llm_timeout_seconds=_float_env("LLM_TIMEOUT_SECONDS", 120.0),
            # Validate-and-retry attempts before falling back to the deterministic ranking.
            llm_max_attempts=_int_env("LLM_MAX_ATTEMPTS", 3),
            # Kept free in the context window for the response (and retry turns).
            llm_response_reserve_tokens=_int_env("LLM_RESPONSE_RESERVE_TOKENS", 3072),
            # Hard cap on generated tokens (Ollama num_predict, OpenRouter max_tokens). Three
            # hypotheses need ~400. Without a cap, schema-constrained decoding was seen to run for
            # over 5 minutes on a prompt that pushed disallowed ids; with it, a runaway reply ends
            # and is rejected as invalid. Reasoning models spend most of this budget thinking -
            # nemotron used 569 of 719 tokens on reasoning - so the default is far above the answer.
            llm_max_output_tokens=_int_env("LLM_MAX_OUTPUT_TOKENS", 3072),
            # Candidates shown to the LLM, trimmed to the minimum when the prompt is over budget.
            prompt_max_candidates=_int_env("PROMPT_MAX_CANDIDATES", 5),
            prompt_min_candidates=_int_env("PROMPT_MIN_CANDIDATES", 3),
            # Default /analyze pipeline mode; a request can override it with ?mode=. The other modes
            # exist for the evaluation's ablations (PLAN.md, Phase 8).
            pipeline_mode=_choice_env("PIPELINE_MODE", "full", ("full", "llm_only", "no_graph", "deterministic")),
        )


settings = Settings.from_env()
