"""Phase 6: reply validation and normalisation, validate-and-retry, and the Ollama chat call.
No real LLM."""

import json

import httpx
import pytest

from app.hypotheses import CandidateOptions
from app.llm import LLMDiagnoser, validate_reply
from app.ollama import ChatReply, OllamaClient, OllamaUnavailable
from app.prompts import Prompt, response_schema

OPTIONS = [
    CandidateOptions(service="catalogue", citable_ids=["anom-1", "dep-1"], actions=["no_action", "rollback_deploy:dep-1"]),
    CandidateOptions(service="user", citable_ids=["anom-1"], actions=["no_action"]),
]
PROMPT = Prompt(
    messages=[{"role": "system", "content": "rules"}, {"role": "user", "content": "facts"}],
    schema=response_schema(OPTIONS),
    options=OPTIONS,
    estimated_tokens=10,
)


def hypothesis(**changes):
    return {
        "rank": 1,
        "service": "catalogue",
        "cause": "catalogue deploy dep-1 landed just before onset",
        "confidence": 0.8,
        "evidence_ids": ["anom-1", "dep-1"],
        "proposed_action": "rollback_deploy:dep-1",
        **changes,
    }


def user_hypothesis(**changes):
    defaults = {
        "service": "user",
        "cause": "user is anomalous",
        "confidence": 0.3,
        "evidence_ids": ["anom-1"],
        "proposed_action": "no_action",
    }
    return hypothesis(**{**defaults, **changes})


def reply(*hypotheses):
    return json.dumps({"hypotheses": list(hypotheses)})


# ------------------------------------------------------------------ validation


def test_a_valid_reply_becomes_the_contract_shape():
    diagnosis, errors = validate_reply(reply(hypothesis(), user_hypothesis(rank=2)), PROMPT)
    assert errors == [] and diagnosis.adjustments == []
    assert diagnosis.services == ["catalogue", "user"]
    first = diagnosis.response.hypotheses[0].model_dump()
    assert "service" not in first  # the contract has no service field
    assert first["proposed_action"] == "rollback_deploy:dep-1"


@pytest.mark.parametrize(
    "content, fragment",
    [
        ("The catalogue is broken.", "Invalid JSON"),
        (reply(hypothesis(confidence=1.7)), "confidence"),
        (reply(hypothesis(service="payment")), "not a listed candidate"),
        (reply(user_hypothesis(proposed_action="rollback_deploy:dep-1")), "user's may-propose list"),
        (reply(), "at least one"),
        (reply(hypothesis(), user_hypothesis(rank=2), hypothesis(rank=3), user_hypothesis(rank=4)), "at most 3"),
        (reply(hypothesis(reasoning="because")), "reasoning"),
        (reply({k: v for k, v in hypothesis().items() if k != "service"}), "service"),
    ],
)
def test_invalid_replies_are_rejected_with_a_reason(content, fragment):
    diagnosis, errors = validate_reply(content, PROMPT)
    assert diagnosis is None
    assert any(fragment in error for error in errors), errors


def test_a_repeated_candidate_is_dropped():
    diagnosis, errors = validate_reply(reply(hypothesis(), hypothesis(rank=2, confidence=0.1), user_hypothesis(rank=3)), PROMPT)
    assert errors == []
    assert diagnosis.services == ["catalogue", "user"]
    assert [h.rank for h in diagnosis.response.hypotheses] == [1, 2]
    assert diagnosis.adjustments == ["dropped a second hypothesis about catalogue"]


def test_rank_is_made_to_follow_confidence():
    diagnosis, errors = validate_reply(reply(user_hypothesis(rank=1, confidence=0.3), hypothesis(rank=2, confidence=0.8)), PROMPT)
    assert errors == []
    assert diagnosis.services == ["catalogue", "user"]
    assert [h.confidence for h in diagnosis.response.hypotheses] == [0.8, 0.3]
    assert diagnosis.adjustments == ["reordered hypotheses so rank follows confidence"]


# ------------------------------------------------------------------ retry loop


class ScriptedChat:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, messages, schema):
        self.calls.append((messages, schema))
        outcome = self.replies[min(len(self.calls), len(self.replies)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, ChatReply):
            return outcome
        return ChatReply(content=outcome, prompt_tokens=321)


def test_first_valid_reply_needs_one_attempt():
    chat = ScriptedChat(reply(hypothesis()))
    outcome = LLMDiagnoser(chat).diagnose(PROMPT)
    assert (outcome.attempts, outcome.errors, outcome.prompt_tokens) == (1, [], 321)
    assert chat.calls[0] == (PROMPT.messages, PROMPT.schema)


def test_a_rejected_reply_is_sent_back_with_the_reason():
    chat = ScriptedChat(reply(user_hypothesis(proposed_action="rollback_deploy:dep-1")), reply(hypothesis()))
    outcome = LLMDiagnoser(chat).diagnose(PROMPT)
    assert outcome.diagnosis is not None and outcome.attempts == 2
    retry_messages = chat.calls[1][0]
    assert retry_messages[:2] == PROMPT.messages
    assert retry_messages[2]["role"] == "assistant" and "rollback_deploy:dep-1" in retry_messages[2]["content"]
    assert retry_messages[3]["role"] == "user" and "may-propose list" in retry_messages[3]["content"]
    assert len(outcome.errors) == 1


def test_a_reply_cut_off_at_the_token_limit_is_explained_on_retry():
    looping = '{"hypotheses": [{"rank": 1, "service": "catalogue", "evidence_ids": ["dep-1", "dep-1", "dep-1",'
    chat = ScriptedChat(ChatReply(content=looping, done_reason="length"), reply(hypothesis()))
    outcome = LLMDiagnoser(chat).diagnose(PROMPT)
    assert outcome.diagnosis is not None and outcome.attempts == 2
    assert "output token limit" in chat.calls[1][0][-1]["content"]
    assert "output token limit" in outcome.errors[0]


def test_gives_up_after_the_last_attempt():
    chat = ScriptedChat("not json")
    outcome = LLMDiagnoser(chat, max_attempts=3).diagnose(PROMPT)
    assert outcome.diagnosis is None and outcome.attempts == 3
    assert len(chat.calls) == 3 and len(outcome.errors) == 3


def test_an_unreachable_ollama_stops_immediately():
    chat = ScriptedChat(OllamaUnavailable("connection refused"))
    outcome = LLMDiagnoser(chat).diagnose(PROMPT)
    assert outcome.diagnosis is None and outcome.attempts == 1
    assert "connection refused" in outcome.errors[0]
    assert len(chat.calls) == 1


def test_max_attempts_must_be_positive():
    with pytest.raises(ValueError):
        LLMDiagnoser(ScriptedChat(), max_attempts=0)


# ------------------------------------------------------------------ Ollama chat


def test_chat_sends_the_schema_and_reads_token_counts():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "message": {"role": "assistant", "content": "{}"},
                "prompt_eval_count": 575,
                "eval_count": 127,
                "done_reason": "stop",
            },
        )

    client = OllamaClient("http://ollama.test", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    options = {"num_ctx": 8192, "temperature": 0.1, "num_predict": 768}
    result = client.chat(PROMPT.messages, "phi4-mini", PROMPT.schema, options, timeout_seconds=5)
    assert result == ChatReply(content="{}", prompt_tokens=575, output_tokens=127, done_reason="stop")
    body = seen[0]
    assert body["format"] == PROMPT.schema and body["stream"] is False
    assert body["options"] == options and body["model"] == "phi4-mini"


def test_chat_without_message_content_is_an_error():
    client = OllamaClient(
        "http://ollama.test", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"done": True})), sleep=lambda s: None
    )
    with pytest.raises(OllamaUnavailable, match="no message content"):
        client.chat(PROMPT.messages, "phi4-mini", "json", {})
