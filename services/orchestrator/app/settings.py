"""Runtime configuration from environment variables. Names and defaults match the rest of
docker-compose.yml (PG_*, KAFKA_BOOTSTRAP), so this service is configured the same way as
metrics-sink, deploy-emitter and diagnosis-service."""

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


@dataclass(frozen=True)
class Settings:
    pg_host: str
    pg_port: int
    pg_db: str
    pg_user: str
    pg_password: str
    kafka_bootstrap: str
    diagnosis_service_url: str
    diagnosis_timeout_seconds: float
    diagnosis_max_attempts: int
    diagnosis_retry_backoff_seconds: float
    # How long an incident may sit in AWAITING_APPROVAL before the sweeper expires it.
    approval_timeout_seconds: float
    # How often the sweeper thread wakes up to check for expired incidents.
    sweep_interval_seconds: float

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            pg_host=os.environ.get("PG_HOST", "timescaledb"),
            pg_port=_int_env("PG_PORT", 5432),
            pg_db=os.environ.get("PG_DB", "metrics"),
            pg_user=os.environ.get("PG_USER", "postgres"),
            pg_password=os.environ.get("PG_PASSWORD", "Abcd1234#"),
            kafka_bootstrap=os.environ.get("KAFKA_BOOTSTRAP", "kafka:9092"),
            diagnosis_service_url=os.environ.get("DIAGNOSIS_SERVICE_URL", "http://diagnosis-service:8000"),
            # M3's own timeout budget for a cold LLM load is ~120s (its LLM_TIMEOUT_SECONDS);
            # leave headroom above that rather than timing out first and racing a retry
            # against a request M3 is still legitimately working on.
            diagnosis_timeout_seconds=_float_env("DIAGNOSIS_TIMEOUT_SECONDS", 150.0),
            diagnosis_max_attempts=_int_env("DIAGNOSIS_MAX_ATTEMPTS", 3),
            diagnosis_retry_backoff_seconds=_float_env("DIAGNOSIS_RETRY_BACKOFF_SECONDS", 5.0),
            approval_timeout_seconds=_float_env("APPROVAL_TIMEOUT_SECONDS", 1800.0),
            sweep_interval_seconds=_float_env("SWEEP_INTERVAL_SECONDS", 15.0),
        )


settings = Settings.from_env()
