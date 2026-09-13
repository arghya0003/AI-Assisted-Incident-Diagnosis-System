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
        )


settings = Settings.from_env()
