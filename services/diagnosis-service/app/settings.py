"""Runtime configuration from environment variables.

Names and defaults match the rest of docker-compose.yml (PG_*, KAFKA_BOOTSTRAP), so this
service is configured the same way as metrics-sink and deploy-emitter.
"""

import os
from dataclasses import dataclass


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


def _choice_env(name: str, default: str, choices: tuple[str, ...]) -> str:
    value = os.environ.get(name) or default
    if value not in choices:
        raise ValueError(f"{name} must be one of {choices}, got {value!r}")
    return value


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be true or false, got {raw!r}")


@dataclass(frozen=True)
class Settings:
    kafka_bootstrap: str
    pg_host: str
    pg_port: int
    pg_db: str
    pg_user: str
    pg_password: str
    ollama_url: str
    llm_model: str
    embed_model: str
    llm_context_tokens: int
    consumer_enabled: bool
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

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            kafka_bootstrap=os.environ.get("KAFKA_BOOTSTRAP", "kafka:9092"),
            pg_host=os.environ.get("PG_HOST", "timescaledb"),
            pg_port=_int_env("PG_PORT", 5432),
            pg_db=os.environ.get("PG_DB", "metrics"),
            pg_user=os.environ.get("PG_USER", "postgres"),
            pg_password=os.environ.get("PG_PASSWORD", "Abcd1234#"),
            # Ollama runs on the host (it needs the GPU), not in a container.
            ollama_url=os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434"),
            llm_model=os.environ.get("LLM_MODEL", "phi4-mini"),
            embed_model=os.environ.get("EMBED_MODEL", "nomic-embed-text"),
            # Phase 0: 8192 keeps prompt room with no measured latency cost over 4096.
            llm_context_tokens=_int_env("LLM_CONTEXT_TOKENS", 8192),
            # The anomalies.detected consumer. Off in unit tests and when running without Kafka.
            consumer_enabled=_bool_env("CONSUMER_ENABLED", True),
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
            llm_response_reserve_tokens=_int_env("LLM_RESPONSE_RESERVE_TOKENS", 1024),
            # Hard cap on generated tokens (Ollama num_predict). Three hypotheses need ~400. Without a
            # cap, schema-constrained decoding was seen to run for over 5 minutes on a prompt that
            # pushed disallowed ids; with it, a runaway reply ends and is rejected as invalid.
            llm_max_output_tokens=_int_env("LLM_MAX_OUTPUT_TOKENS", 768),
            # Candidates shown to the LLM, trimmed to the minimum when the prompt is over budget.
            prompt_max_candidates=_int_env("PROMPT_MAX_CANDIDATES", 5),
            prompt_min_candidates=_int_env("PROMPT_MIN_CANDIDATES", 3),
        )


settings = Settings.from_env()
