"""Application configuration management via pydantic-settings."""

import json
from functools import lru_cache
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings with environment variable loading and validation."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Core environment
    ENVIRONMENT: str = "development"
    DATABASE_URL: str = "postgresql+psycopg://sma:sma@127.0.0.1:15432/sma"
    TEST_DATABASE_URL: str = "postgresql+psycopg://sma:sma@127.0.0.1:15432/sma_test"
    REDIS_URL: str = "redis://localhost:6379/0"
    SECRET_KEY: str = "change-me"
    ACCESS_TOKEN_MINUTES: int = 30
    REFRESH_TOKEN_DAYS: int = 14
    FERNET_KEY: str = "generate-with-cryptography"
    CORS_ORIGINS: list[str] | str = ["http://localhost:3000"]
    LOG_LEVEL: str = "INFO"

    # LLM and AI Providers
    LLM_PROVIDER: str = "fake"
    GEMINI_API_KEY: str | None = None
    LLM_MODEL_TEXT: str | None = None
    LLM_MODEL_FAST: str | None = None
    IMAGE_PROVIDER: str = "fake"
    VIDEO_PROVIDER: str = "fake"
    TTS_PROVIDER: str = "fake"
    SEARCH_PROVIDER: str = "fake"

    # Storage
    STORAGE_BACKEND: str = "local"
    STORAGE_PATH: str = "./storage"
    S3_ENDPOINT: str | None = None
    S3_BUCKET: str | None = None
    S3_ACCESS_KEY: str | None = None
    S3_SECRET_KEY: str | None = None

    # Publishers & OAuth
    PUBLISHER_MODE: str = "mock"
    FACEBOOK_APP_ID: str | None = None
    FACEBOOK_APP_SECRET: str | None = None
    GOOGLE_CLIENT_ID: str | None = None
    GOOGLE_CLIENT_SECRET: str | None = None
    TIKTOK_CLIENT_KEY: str | None = None
    TIKTOK_CLIENT_SECRET: str | None = None

    # Password reset
    PASSWORD_RESET_MINUTES: int = 60
    PASSWORD_RESET_BASE_URL: str = "http://localhost:3000/reset-password"
    # Rate limiting: fixed-window counters stored in Redis
    PW_RESET_IP_LIMIT: int = 5
    PW_RESET_IP_WINDOW_SECONDS: int = 600  # 10 minutes
    PW_RESET_ACCT_LIMIT: int = 3
    PW_RESET_ACCT_WINDOW_SECONDS: int = 900  # 15 minutes
    PW_RESET_COOLDOWN_SECONDS: int = 60  # min gap between requests per account

    # Email provider (console | smtp)
    EMAIL_PROVIDER: str = "console"

    # Seeding (required for make seed; credentials must be in environment)
    SEED_ADMIN_EMAIL: str | None = None
    SEED_ADMIN_PASSWORD: str | None = None

    @field_validator("CORS_ORIGINS")
    @classmethod
    def parse_cors_origins(cls, value: Any) -> list[str]:
        """Parse CORS origins from string, JSON array, or list."""
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return []
            if value.startswith("[") and value.endswith("]"):
                try:
                    parsed = json.loads(value)
                    if isinstance(parsed, list):
                        return [
                            str(item).strip() for item in parsed if str(item).strip()
                        ]
                except Exception:
                    pass
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings instance."""
    return Settings()


settings = get_settings()
