"""OpenRouter chat client: the default generation provider.

Why it replaced Ollama as the default: phi4-mini ran only on a machine with the model pulled and a
GPU to spare, which no other machine on the team had, so every integration run answered
`deterministic_fallback`. The deployed system is online anyway - it diagnoses a running website -
so an API is the honest dependency.

The same response schema is sent as to Ollama, wrapped in OpenAI's `response_format: json_schema`
with `strict: true`. That was verified before this client was written: the per-candidate `anyOf`
design, which binds each hypothesis to one service's evidence ids and actions, survives the move
and validated first try on nvidia/nemotron-3-super-120b-a12b.

Two failures are deliberately distinguished, because conflating them would corrupt the evaluation:

  * Availability - HTTP 429, 5xx, timeouts, a model that is unreachable. The caller may try the
    next model in the chain, and the answer stays attributable to whichever model produced it.
  * Quality - a reply that parses but breaks the contract. That is the model's own behaviour and
    is retried against the *same* model by app/llm.py, never silently handed to another one.
"""

import logging
import time
from collections.abc import Callable

import httpx

from app.providers import ChatReply, ProviderUnavailable

log = logging.getLogger("diagnosis-service.openrouter")

DEFAULT_URL = "https://openrouter.ai/api/v1"
# Sent so usage shows up under a recognisable name on the OpenRouter dashboard. Optional, and
# nothing depends on it.
REFERER = "https://github.com/arghya0003/AI-Assisted-Incident-Diagnosis-System"
APP_TITLE = "AI-Assisted Incident Diagnosis System"


class NoKey(ProviderUnavailable):
    """OPENROUTER_API_KEY is unset. Its own type so the log line can say so plainly, rather than
    surfacing as a 401 that looks like a broken key."""


class OpenRouterClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_URL,
        timeout_seconds: float = 120.0,
        attempts: int = 3,
        backoff_seconds: float = 2.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._api_key = api_key
        self._client = httpx.Client(base_url=base_url, timeout=timeout_seconds, transport=transport)
        self._attempts = attempts
        self._backoff_seconds = backoff_seconds
        self._sleep = sleep

    def chat(
        self,
        messages: list[dict[str, str]],
        model: str,
        schema: dict,
        temperature: float,
        max_output_tokens: int,
        reasoning_effort: str | None = None,
        timeout_seconds: float | None = None,
    ) -> ChatReply:
        if not self._api_key:
            raise NoKey("OPENROUTER_API_KEY is not set; put it in the gitignored .env at the repo root")
        body: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_output_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "hypotheses", "strict": True, "schema": schema},
            },
        }
        if reasoning_effort:
            # Measured on nemotron: 569 of 719 output tokens were reasoning for a single
            # hypothesis. Left uncapped it crowds out the answer inside max_tokens.
            body["reasoning"] = {"effort": reasoning_effort}
        payload = self._post("/chat/completions", body, timeout_seconds)

        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderUnavailable(f"{model} returned no choices")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            # A reasoning model that spends its whole budget thinking returns an empty content with
            # finish_reason "length". That is an availability problem, not a contract violation.
            raise ProviderUnavailable(
                f"{model} returned empty content (finish_reason={choices[0].get('finish_reason')!r}); "
                "raise LLM_MAX_OUTPUT_TOKENS or lower LLM_REASONING_EFFORT"
            )
        usage = payload.get("usage") or {}
        return ChatReply(
            content=content,
            prompt_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            done_reason=choices[0].get("finish_reason"),
            # What the response says served the request, not what was asked for: OpenRouter can
            # route elsewhere, and the evaluation needs the model that actually answered.
            model=payload.get("model") or model,
        )

    def _post(self, path: str, payload: dict, timeout_seconds: float | None = None) -> dict:
        timeout = httpx.USE_CLIENT_DEFAULT if timeout_seconds is None else timeout_seconds
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": REFERER,
            "X-Title": APP_TITLE,
        }
        failure = ""
        for attempt in range(1, self._attempts + 1):
            try:
                response = self._client.post(path, json=payload, headers=headers, timeout=timeout)
            except httpx.ReadTimeout as exc:
                raise ProviderUnavailable(f"{path} timed out waiting for a response: {exc}") from exc
            except httpx.TransportError as exc:
                failure = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code == 200:
                    return _decode(path, response)
                detail = _error_detail(response)
                # 401/403 mean the key is wrong or the account's data policy blocks this model
                # (free models need prompt logging enabled). 404 means no endpoint matches.
                # Retrying cannot fix any of them.
                #
                # 429 is not retried here either, deliberately. A rate limit clears on someone
                # else's schedule, and the free tier allows 50 requests a day, so waiting two
                # seconds and spending another one is the wrong trade: the caller's fallback chain
                # switches to a different model immediately and for free.
                if response.status_code in (400, 401, 403, 404, 429):
                    raise ProviderUnavailable(f"{path} returned HTTP {response.status_code}: {detail}")
                failure = f"HTTP {response.status_code}: {detail}"
            if attempt < self._attempts:
                log.warning("openrouter %s attempt %d/%d failed (%s); retrying", path, attempt, self._attempts, failure)
                self._sleep(self._backoff_seconds * attempt)
        raise ProviderUnavailable(f"{path} failed after {self._attempts} attempts: {failure}")


def _decode(path: str, response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError as exc:
        raise ProviderUnavailable(f"{path} returned unparseable JSON: {response.text[:200]}") from exc
    if not isinstance(body, dict):
        raise ProviderUnavailable(f"{path} returned {type(body).__name__}, expected a JSON object")
    # OpenRouter reports some upstream failures as HTTP 200 with an error object in the body.
    error = body.get("error")
    if error:
        raise ProviderUnavailable(f"{path} returned an error body: {str(error)[:300]}")
    return body


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        message = error.get("message") or ""
        metadata = error.get("metadata") or {}
        raw = metadata.get("raw") if isinstance(metadata, dict) else None
        return f"{message} {raw or ''}".strip()[:300]
    return str(body)[:200]
