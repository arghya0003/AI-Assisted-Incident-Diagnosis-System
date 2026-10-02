"""Settings parsed from the environment, where a mistake is someone else's confusing afternoon."""

from app.settings import Settings


def test_an_api_key_pasted_with_stray_whitespace_is_usable(monkeypatch):
    """A key with a trailing space becomes an illegal HTTP header value, and httpx raises
    LocalProtocolError rather than anything resembling "your key has a space in it". docker compose
    strips it while `docker run -e` does not, so the same .env file worked in one place and failed
    in another - which is how this was found."""
    monkeypatch.setenv("GEMINI_API_KEY", "  abc123\n")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-xyz ")
    settings = Settings.from_env()
    assert settings.gemini_api_key == "abc123"
    assert settings.openrouter_api_key == "sk-or-v1-xyz"


def test_an_absent_api_key_is_empty_rather_than_none(monkeypatch):
    """Empty, so `if not settings.gemini_api_key` reads naturally and nothing has to guard None."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert Settings.from_env().gemini_api_key == ""


def test_the_default_model_follows_the_provider(monkeypatch):
    """So that switching provider does not also require remembering to switch LLM_MODEL."""
    monkeypatch.delenv("LLM_MODEL", raising=False)
    for provider, expected in (("gemini", "gemini-flash-latest"), ("ollama", "phi4-mini")):
        monkeypatch.setenv("LLM_PROVIDER", provider)
        assert Settings.from_env().llm_model == expected
