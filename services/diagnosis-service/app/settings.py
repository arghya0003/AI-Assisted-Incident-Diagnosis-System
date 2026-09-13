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
        )


settings = Settings.from_env()
