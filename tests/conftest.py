"""Shared pytest fixtures."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from app.agents.llm import FakeScript, get_fake_script
from app.core.config import get_settings
from app.providers.image import FakeImage, get_fake_image


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


@pytest.fixture
def fake_image(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeImage]:
    """Force the offline image provider and hand out its (clean) shared instance."""
    monkeypatch.setenv("IMAGE_PROVIDER", "fake")
    get_settings.cache_clear()
    provider = get_fake_image()
    provider.reset()
    try:
        yield provider
    finally:
        provider.reset()
        get_settings.cache_clear()


@pytest.fixture
def media_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Point ``get_storage_backend()`` at a fresh local directory."""
    root = tmp_path / "media"
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("STORAGE_PATH", str(root))
    get_settings.cache_clear()
    try:
        yield root
    finally:
        get_settings.cache_clear()
