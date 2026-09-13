"""Minimal Ollama HTTP client: embeddings now, generation in Phase 6.

Transport errors and 5xx responses are retried with linear backoff: in Phase 0 the first model
load crashed Ollama's runner once (HTTP 500) and the immediate retry succeeded. 4xx responses,
such as a model that isn't pulled, are not retried because retrying cannot fix them.
"""

import logging
import time
from collections.abc import Callable

import httpx

log = logging.getLogger("diagnosis-service.ollama")


class OllamaUnavailable(RuntimeError):
    """Ollama could not produce a usable response."""


class OllamaClient:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 60.0,
        attempts: int = 3,
        backoff_seconds: float = 1.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._client = httpx.Client(base_url=base_url, timeout=timeout_seconds, transport=transport)
        self._attempts = attempts
        self._backoff_seconds = backoff_seconds
        self._sleep = sleep

    def embed(self, texts: list[str], model: str) -> list[list[float]]:
        body = self._post("/api/embed", {"model": model, "input": texts})
        embeddings = body.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
            count = len(embeddings) if isinstance(embeddings, list) else None
            raise OllamaUnavailable(f"/api/embed returned {count} embeddings for {len(texts)} inputs")
        return embeddings

    def _post(self, path: str, payload: dict) -> dict:
        failure = ""
        for attempt in range(1, self._attempts + 1):
            try:
                response = self._client.post(path, json=payload)
            except httpx.TransportError as exc:
                failure = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code < 500:
                    if response.is_error:
                        raise OllamaUnavailable(f"{path} returned HTTP {response.status_code}: {response.text[:200]}")
                    return response.json()
                failure = f"HTTP {response.status_code}: {response.text[:200]}"
            if attempt < self._attempts:
                log.warning("ollama %s attempt %d/%d failed (%s); retrying", path, attempt, self._attempts, failure)
                self._sleep(self._backoff_seconds * attempt)
        raise OllamaUnavailable(f"{path} failed after {self._attempts} attempts: {failure}")
