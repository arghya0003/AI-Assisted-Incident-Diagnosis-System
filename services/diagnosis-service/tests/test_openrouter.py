"""The OpenRouter provider and its model fallback chain. No network."""

import dataclasses
import json

import httpx
import pytest

from app.hypotheses import CandidateOptions
from app.openrouter import NoKey, OpenRouterClient
from app.pipeline import build_chat, openrouter_chat
from app.prompts import response_schema
from app.providers import ProviderUnavailable
from app.settings import Settings

MESSAGES = [{"role": "system", "content": "rules"}, {"role": "user", "content": "facts"}]
SCHEMA = response_schema(
    [CandidateOptions(service="catalogue", citable_ids=["anom-1"], actions=["no_action"], has_recent_deploy=False)]
)
ANSWER = {"hypotheses": [{"rank": 1, "service": "catalogue", "cause": "c", "confidence": 0.5,
                          "evidence_ids": ["anom-1"], "proposed_action": "no_action"}]}


def reply_body(model="primary/model", content=None, finish="stop"):
    return {
        "model": model,
        "choices": [{"finish_reason": finish, "message": {"content": json.dumps(ANSWER) if content is None else content}}],
        "usage": {"prompt_tokens": 800, "completion_tokens": 120},
    }


def client_returning(*outcomes, calls=None):
    """Each outcome is an httpx.Response, an Exception, or a status code."""
    calls = calls if calls is not None else []

    def handler(request):
        calls.append(json.loads(request.content))
        outcome = outcomes[min(len(calls), len(outcomes)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, httpx.Response):
            return outcome
        return httpx.Response(outcome, json={"error": {"message": "upstream said no"}})

    return OpenRouterClient("test-key", transport=httpx.MockTransport(handler), sleep=lambda s: None), calls


def settings_with(**changes) -> Settings:
    base = Settings.from_env()
    return dataclasses.replace(base, **changes)


def call(client, model="primary/model"):
    return client.chat(MESSAGES, model, SCHEMA, temperature=0.1, max_output_tokens=3072)


# ---------------------------------------------------------------- the client


def test_sends_the_schema_as_a_strict_json_schema():
    """The per-candidate anyOf design is the safety mechanism; it must reach the provider intact."""
    client, calls = client_returning(httpx.Response(200, json=reply_body()))
    call(client)
    body = calls[0]
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"] == SCHEMA
    assert body["max_tokens"] == 3072


def test_reports_which_model_answered():
    client, _ = client_returning(httpx.Response(200, json=reply_body()))
    reply = call(client)
    assert reply.model == "primary/model"
    assert reply.prompt_tokens == 800 and reply.output_tokens == 120


def test_a_missing_key_says_so_rather_than_failing_as_a_401():
    client = OpenRouterClient("", transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with pytest.raises(NoKey, match="OPENROUTER_API_KEY"):
        call(client)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429])
def test_client_errors_are_not_retried(status):
    """A bad key, a data policy that blocks free models, or no matching endpoint: retrying cannot
    fix any of them. Nor is a 429 retried here - a rate limit clears on someone else's schedule,
    and on a 50-request daily budget switching model is instant and free where waiting is neither.
    """
    client, calls = client_returning(status)
    with pytest.raises(ProviderUnavailable):
        call(client)
    assert len(calls) == 1


def test_an_error_body_behind_http_200_is_still_a_failure():
    client, _ = client_returning(httpx.Response(200, json={"error": {"message": "upstream exploded"}}))
    with pytest.raises(ProviderUnavailable, match="upstream exploded"):
        call(client)


def test_an_empty_answer_from_a_reasoning_model_is_unavailable_not_invalid():
    """A reasoning model that spends the whole budget thinking returns empty content. That is a
    budget problem to report, not a contract violation to retry against the same prompt."""
    client, _ = client_returning(httpx.Response(200, json=reply_body(content="", finish="length")))
    with pytest.raises(ProviderUnavailable, match="LLM_MAX_OUTPUT_TOKENS"):
        call(client)


def test_unparseable_success_is_unavailable():
    client, _ = client_returning(httpx.Response(200, text="<html>gateway</html>"))
    with pytest.raises(ProviderUnavailable, match="unparseable"):
        call(client)


def test_reasoning_effort_is_sent_only_when_set():
    client, calls = client_returning(httpx.Response(200, json=reply_body()), httpx.Response(200, json=reply_body()))
    client.chat(MESSAGES, "m", SCHEMA, temperature=0.1, max_output_tokens=100, reasoning_effort="low")
    assert calls[0]["reasoning"] == {"effort": "low"}
    client.chat(MESSAGES, "m", SCHEMA, temperature=0.1, max_output_tokens=100, reasoning_effort=None)
    assert "reasoning" not in calls[1]


# ---------------------------------------------------------------- the fallback chain


def test_an_unavailable_model_falls_through_to_the_next():
    """A rate-limited primary must move to the fallback immediately, not burn its retries first."""
    client, calls = client_returning(429, httpx.Response(200, json=reply_body(model="backup/model")))
    settings = settings_with(llm_model="primary/model", llm_fallback_models=("backup/model",))
    reply = openrouter_chat(client, settings)(MESSAGES, SCHEMA)
    assert reply.model == "backup/model", "the answer must be attributed to the model that produced it"
    assert [c["model"] for c in calls] == ["primary/model", "backup/model"], "one request per model"


def test_transport_failures_are_still_retried_on_the_same_model():
    """Unlike a rate limit, a dropped connection is worth retrying where it happened."""
    client, calls = client_returning(httpx.ConnectError("connection reset"), httpx.Response(200, json=reply_body()))
    assert call(client).model == "primary/model"
    assert len(calls) == 2


def test_server_errors_are_retried_then_reported():
    client, calls = client_returning(503)
    with pytest.raises(ProviderUnavailable, match="3 attempts"):
        call(client)
    assert len(calls) == 3


def test_every_model_unavailable_reports_all_of_them():
    client, _ = client_returning(429)
    settings = settings_with(llm_model="primary/model", llm_fallback_models=("backup/model",))
    with pytest.raises(ProviderUnavailable) as exc:
        openrouter_chat(client, settings)(MESSAGES, SCHEMA)
    assert "primary/model" in str(exc.value) and "backup/model" in str(exc.value)


def test_the_primary_is_not_repeated_when_it_is_also_listed_as_a_fallback():
    client, calls = client_returning(429)
    settings = settings_with(llm_model="same/model", llm_fallback_models=("same/model",))
    with pytest.raises(ProviderUnavailable):
        openrouter_chat(client, settings)(MESSAGES, SCHEMA)
    assert {c["model"] for c in calls} == {"same/model"}
    assert len(calls) == 1, "a rate-limited model is tried once, and a duplicate entry adds nothing"


def test_build_chat_honours_the_configured_provider():
    ollama_only = settings_with(llm_provider="ollama")
    assert build_chat(ollama_only, ollama=_FakeOllama(), openrouter=None) is not None


class _FakeOllama:
    def chat(self, *args, **kwargs):  # pragma: no cover - only identity matters here
        raise AssertionError("not called")
