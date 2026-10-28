import pytest


@pytest.fixture(autouse=True)
def no_ambient_openrouter_key(monkeypatch):
    """No test may reach the live OpenRouter API.

    `uv run` loads `.env`, so a developer's real OPENROUTER_API_KEY is present
    in the test process. Without this, `digest.send_digest` called the network:
    the suite went from 0.3s to 105s and two tests failed on the extra message
    the analysis produced.

    Tests that want a key present set it themselves with `monkeypatch.setenv`,
    and every one of those also replaces `openrouter_client.analyze`.
    """
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
