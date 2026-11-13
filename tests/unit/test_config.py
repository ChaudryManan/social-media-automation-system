"""Tests for configuration settings and environment variable overrides."""

import pytest
from app.core.config import Settings


def test_config_default_values() -> None:
    """Settings should load typed sensible defaults with no env overrides."""
    settings = Settings(
        _env_file=None,
    )
    assert settings.ENVIRONMENT == "development"
    assert "postgresql+psycopg://" in settings.DATABASE_URL
    assert settings.REDIS_URL.startswith("redis://")
    assert settings.ACCESS_TOKEN_MINUTES == 30
    assert settings.REFRESH_TOKEN_DAYS == 14
    assert settings.LLM_PROVIDER == "fake"
    assert settings.PUBLISHER_MODE == "mock"
    assert settings.CORS_ORIGINS == ["http://localhost:3000"]
    assert settings.STORAGE_BACKEND == "local"


def test_config_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings should correctly reflect environment variable overrides."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("ACCESS_TOKEN_MINUTES", "45")
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv(
        "CORS_ORIGINS", "https://app.example.com,https://admin.example.com"
    )
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    settings = Settings(_env_file=None)
    assert settings.ENVIRONMENT == "production"
    assert settings.ACCESS_TOKEN_MINUTES == 45
    assert settings.LLM_PROVIDER == "gemini"
    assert settings.CORS_ORIGINS == [
        "https://app.example.com",
        "https://admin.example.com",
    ]
    assert settings.LOG_LEVEL == "DEBUG"


def test_cors_origins_parsing_variants() -> None:
    """CORS_ORIGINS should handle comma strings, JSON arrays, and lists."""
    # Comma-separated string
    s1 = Settings(_env_file=None, CORS_ORIGINS="http://a.com, http://b.com")
    assert s1.CORS_ORIGINS == ["http://a.com", "http://b.com"]

    # JSON array string
    s2 = Settings(_env_file=None, CORS_ORIGINS='["http://c.com", "http://d.com"]')
    assert s2.CORS_ORIGINS == ["http://c.com", "http://d.com"]

    # Direct list
    s3 = Settings(_env_file=None, CORS_ORIGINS=["http://e.com"])
    assert s3.CORS_ORIGINS == ["http://e.com"]

    # Empty string
    s4 = Settings(_env_file=None, CORS_ORIGINS="   ")
    assert s4.CORS_ORIGINS == []
