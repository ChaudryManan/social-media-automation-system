"""Shared pytest fixtures."""

from collections.abc import Iterator

import pytest
from app.agents.llm import FakeScript, get_fake_script
from app.core.config import get_settings


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeScript]:
    """Force the offline fake LLM and hand out its (clean) script.

    A developer's ``.env`` may select a real provider (``.env.example`` sets
    ``LLM_PROVIDER=gemini``), so tests that reach ``get_chat_model`` must use
    this fixture. Environment variables win over ``.env``; the model names are
    blanked so the fake provider uses its own ``fake-<kind>`` labels.
    """
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("LLM_MODEL_TEXT", "")
    monkeypatch.setenv("LLM_MODEL_FAST", "")
    get_settings.cache_clear()
    script = get_fake_script()
    script.reset()
    try:
        yield script
    finally:
        script.reset()
        get_settings.cache_clear()
