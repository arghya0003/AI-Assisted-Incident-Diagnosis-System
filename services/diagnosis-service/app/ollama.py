"""Minimal Ollama HTTP client: embeddings for retrieval, chat for diagnosis.

Transport errors and 5xx responses are retried with linear backoff: in Phase 0 the first model
load crashed Ollama's runner once (HTTP 500) and the immediate retry succeeded. 4xx responses,
such as a model that isn't pulled, and read timeouts are not retried, because retrying cannot fix
the first and would only repeat the wait for the second.
"""

import logging
import time
from collections.abc import Callable

import httpx

from app.providers import ChatReply, ProviderUnavailable

__all__ = ["ChatReply", "OllamaClient", "OllamaUnavailable", "ProviderUnavailable"]

log = logging.getLogger("diagnosis-service.ollama")


class OllamaUnavailable(ProviderUnavailable):
    """Ollama could not produce a usable response. A ProviderUnavailable, so the pipeline handles
    an absent Ollama and an absent API the same way."""


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

    def chat(
        self,
        messages: list[dict[str, str]],
        model: str,
        response_format: dict | str,
        options: dict,
        timeout_seconds: float | None = None,
    ) -> ChatReply:
        """One non-streaming chat completion. `response_format` is "json" or a JSON schema, which
        Ollama enforces while decoding."""
        body = self._post(
            "/api/chat",
            {"model": model, "messages": messages, "stream": False, "format": response_format, "options": options},
            timeout_seconds,
        )
        content = (body.get("message") or {}).get("content")
        if not isinstance(content, str):
            raise OllamaUnavailable("/api/chat response has no message content")
        return ChatReply(
            content=content,
            prompt_tokens=body.get("prompt_eval_count"),
            output_tokens=body.get("eval_count"),
            done_reason=body.get("done_reason"),
        )

    @staticmethod
    def _decode(path: str, response: httpx.Response) -> dict:
        """A 2xx body that is not a JSON object is a protocol failure, not a Python error. Callers
        degrade on OllamaUnavailable only, so an unparseable success would otherwise surface as a
        500 from /analyze instead of the documented fallback."""
        try:
            body = response.json()
        except ValueError as exc:
            raise OllamaUnavailable(f"{path} returned unparseable JSON: {response.text[:200]}") from exc
        if not isinstance(body, dict):
            raise OllamaUnavailable(f"{path} returned {type(body).__name__}, expected a JSON object")
        return body

    def _post(self, path: str, payload: dict, timeout_seconds: float | None = None) -> dict:
        timeout = httpx.USE_CLIENT_DEFAULT if timeout_seconds is None else timeout_seconds
        failure = ""
        for attempt in range(1, self._attempts + 1):
            try:
                response = self._client.post(path, json=payload, timeout=timeout)
            except httpx.ReadTimeout as exc:
                # Not retried: a generation that already ran for the whole timeout would most likely
                # just burn another one.
                raise OllamaUnavailable(f"{path} timed out waiting for a response: {exc}") from exc
            except httpx.TransportError as exc:
                failure = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code < 500:
                    if response.is_error:
                        raise OllamaUnavailable(f"{path} returned HTTP {response.status_code}: {response.text[:200]}")
                    return self._decode(path, response)
                failure = f"HTTP {response.status_code}: {response.text[:200]}"
            if attempt < self._attempts:
                log.warning("ollama %s attempt %d/%d failed (%s); retrying", path, attempt, self._attempts, failure)
                self._sleep(self._backoff_seconds * attempt)
        raise OllamaUnavailable(f"{path} failed after {self._attempts} attempts: {failure}")
