"""What every generation provider has in common.

Two providers exist: OpenRouter (the default, app/openrouter.py) and Ollama (app/ollama.py, kept
selectable so the recorded phi4-mini results stay reproducible and the system can be demonstrated
without an API key). They share a reply shape and a failure type so the pipeline does not care
which one answered.

These live here rather than in either client because both import them, and a shared type owned by
one of the two would be a circular import waiting to happen.
"""

from dataclasses import dataclass


class ProviderUnavailable(RuntimeError):
    """A generation provider produced no answer: unreachable, rate limited, timed out, or
    refusing the request for a reason a retry cannot fix.

    Deliberately distinct from a reply that arrives and breaks the contract. That is the model's
    own behaviour, is retried against the same model (app/llm.py), and must never be silently
    handed to a different model, or a result could no longer be attributed to the model that
    produced it.
    """


@dataclass(frozen=True)
class ChatReply:
    content: str
    prompt_tokens: int | None = None  # the provider's own count, used to check the prompt estimate
    output_tokens: int | None = None
    done_reason: str | None = None  # "length" means generation stopped at the output cap
    # Which model actually produced this reply. With a fallback chain that is not always the
    # configured one, and the evaluation cannot attribute a result without knowing.
    model: str | None = None
