"""Keep the existing suite on the local-Ollama code path regardless of the
developer's .env (which may now contain an OPENROUTER_API_KEY). OpenRouter
behaviour is covered separately in test_openrouter_provider.py."""
import pytest

from ai import llm_client
from config.settings import settings


@pytest.fixture(autouse=True)
def _default_provider_is_ollama(monkeypatch):
    llm_client.reset_cooldown()  # failover cooldown is process-wide state
    llm_client.clear_mode_override()  # UI-chosen engine mode is process-wide state
    monkeypatch.setattr(settings, "LLM_PROVIDER", "ollama")
