"""The OpenRouter provider and its model fallback chain. No network."""

import dataclasses
import json

import httpx
import pytest

from app.hypotheses import CandidateOptions
from app.openrouter import NoKey, OpenRouterClient
from app.pipeline import api_chat, build_chat, chat_chain
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


def test_a_missing_key_names_the_provider_that_needs_it():
    """One client class serves several providers, so a hardcoded variable name sends someone to set
    the wrong one. A missing GEMINI_API_KEY reported "OPENROUTER_API_KEY is not set", which is how
    this was found."""
    client = OpenRouterClient("", transport=httpx.MockTransport(lambda r: httpx.Response(200)),
                              key_name="GEMINI_API_KEY")
    with pytest.raises(NoKey, match="GEMINI_API_KEY is not set"):
        call(client)


def test_each_provider_in_the_chain_names_its_own_key():
    settings = settings_with(llm_provider="gemini", gemini_api_key="", openrouter_api_key="",
                             llm_model="gemini-flash-latest",
                             llm_fallback_models=("openrouter:nvidia/nemotron:free",))
    with pytest.raises(ProviderUnavailable) as exc:
        api_chat(settings)(MESSAGES, SCHEMA)
    message = str(exc.value)
    assert "GEMINI_API_KEY is not set" in message and "OPENROUTER_API_KEY is not set" in message


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


def test_the_thinking_budget_is_spelled_the_way_each_provider_expects():
    """Not cosmetic. Gemini rejects OpenRouter's `reasoning` object with HTTP 400 'Unknown name
    "reasoning"', despite its documentation saying unknown parameters are ignored - so sending the
    wrong spelling makes every request to that provider fail."""
    client, calls = client_returning(httpx.Response(200, json=reply_body()), httpx.Response(200, json=reply_body()))
    client.chat(MESSAGES, "m", SCHEMA, temperature=0.1, max_output_tokens=100,
                reasoning_effort="low", reasoning_style="openai")
    assert calls[0]["reasoning_effort"] == "low" and "reasoning" not in calls[0]
    client.chat(MESSAGES, "m", SCHEMA, temperature=0.1, max_output_tokens=100,
                reasoning_effort="low", reasoning_style="openrouter")
    assert calls[1]["reasoning"] == {"effort": "low"} and "reasoning_effort" not in calls[1]


def test_each_provider_in_the_chain_gets_its_own_spelling():
    gemini, gemini_calls = client_returning(httpx.Response(200, json=reply_body(model="gemini-flash-latest")))
    settings = settings_with(llm_provider="gemini", llm_model="gemini-flash-latest",
                             llm_fallback_models=(), llm_reasoning_effort="low")
    api_chat(settings, {"gemini": gemini})(MESSAGES, SCHEMA)
    assert gemini_calls[0]["reasoning_effort"] == "low", "gemini must get the OpenAI spelling"


# ---------------------------------------------------------------- the fallback chain


def test_an_unavailable_model_falls_through_to_the_next():
    """A rate-limited primary must move to the fallback immediately, not burn its retries first."""
    client, calls = client_returning(429, httpx.Response(200, json=reply_body(model="backup/model")))
    settings = settings_with(llm_provider="openrouter", llm_model="primary/model",
                             llm_fallback_models=("backup/model",))
    reply = api_chat(settings, {"openrouter": client})(MESSAGES, SCHEMA)
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
    settings = settings_with(llm_provider="openrouter", llm_model="primary/model",
                             llm_fallback_models=("backup/model",))
    with pytest.raises(ProviderUnavailable) as exc:
        api_chat(settings, {"openrouter": client})(MESSAGES, SCHEMA)
    assert "primary/model" in str(exc.value) and "backup/model" in str(exc.value)


def test_the_primary_is_not_repeated_when_it_is_also_listed_as_a_fallback():
    client, calls = client_returning(429)
    settings = settings_with(llm_provider="openrouter", llm_model="same/model",
                             llm_fallback_models=("same/model",))
    with pytest.raises(ProviderUnavailable):
        api_chat(settings, {"openrouter": client})(MESSAGES, SCHEMA)
    assert {c["model"] for c in calls} == {"same/model"}
    assert len(calls) == 1, "a rate-limited model is tried once, and a duplicate entry adds nothing"


def test_build_chat_honours_the_configured_provider():
    ollama_only = settings_with(llm_provider="ollama")
    assert build_chat(ollama_only, ollama=_FakeOllama(), clients=None) is not None


# ---------------------------------------------------------------- the cross-vendor chain


def test_a_fallback_entry_may_name_another_provider():
    """The point of the chain: Gemini returns transient 503s under load while OpenRouter exhausts a
    daily quota, so a chain that cannot cross vendors is barely a fallback at all."""
    settings = settings_with(llm_provider="gemini", llm_model="gemini-flash-latest",
                             llm_fallback_models=("openrouter:nvidia/nemotron:free",))
    assert chat_chain(settings) == [("gemini", "gemini-flash-latest"),
                                    ("openrouter", "nvidia/nemotron:free")]


def test_an_entry_without_a_provider_stays_on_the_primary():
    settings = settings_with(llm_provider="openrouter", llm_model="a/model",
                             llm_fallback_models=("b/model",))
    assert chat_chain(settings) == [("openrouter", "a/model"), ("openrouter", "b/model")]


def test_a_colon_in_a_model_name_is_not_mistaken_for_a_provider():
    """OpenRouter's free models end in ":free", so splitting on the first colon unconditionally
    would read "qwen/qwen3.8-27b" as a provider name and lose the model."""
    settings = settings_with(llm_provider="openrouter", llm_model="a/model",
                             llm_fallback_models=("qwen/qwen3.8-27b:free",))
    assert chat_chain(settings) == [("openrouter", "a/model"), ("openrouter", "qwen/qwen3.8-27b:free")]


def test_the_chain_crosses_vendors_when_the_primary_is_overloaded():
    gemini, gemini_calls = client_returning(503)
    openrouter, _ = client_returning(httpx.Response(200, json=reply_body(model="nvidia/nemotron:free")))
    settings = settings_with(llm_provider="gemini", llm_model="gemini-flash-latest",
                             llm_fallback_models=("openrouter:nvidia/nemotron:free",))
    reply = api_chat(settings, {"gemini": gemini, "openrouter": openrouter})(MESSAGES, SCHEMA)
    assert reply.model == "nvidia/nemotron:free"
    assert len(gemini_calls) == 3, "a 503 is transient, so it is retried on the model before moving on"


class _FakeOllama:
    def chat(self, *args, **kwargs):  # pragma: no cover - only identity matters here
        raise AssertionError("not called")
