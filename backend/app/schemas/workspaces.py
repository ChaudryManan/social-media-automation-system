"""Pydantic schemas for workspace requests and responses."""

import json
import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Platform = Literal["facebook", "instagram", "tiktok", "youtube"]

LANGUAGE_REGEX = re.compile(r"^[A-Za-z][A-Za-z0-9-]{1,9}$")
MAX_SETTINGS_JSON_CHARS = 10_000
NON_NULLABLE_UPDATE_FIELDS = ("name", "default_language", "timezone")

WORKSPACE_EXAMPLE = {
    "id": "7b0e2f9c-52d4-4a0e-8c3b-1f6a9d4e2b77",
    "owner_id": "3f1c9a4e-6b1d-4c2a-9a55-0d2f7e8b1c10",
    "name": "Karachi Cafe",
    "description": "Neighbourhood coffee shop",
    "brand_voice": "Friendly, short sentences, light humour",
    "default_language": "ur",
    "timezone": "Asia/Karachi",
    "default_platforms": ["instagram", "facebook"],
    "settings": {"posting_hours": [9, 18]},
    "is_suspended": False,
    "created_at": "2026-11-14T09:30:00Z",
    "updated_at": "2026-11-14T09:30:00Z",
}


class _WorkspaceFieldValidators(BaseModel):
    """Shared normalisation and validation rules for workspace input fields."""

    # Unknown fields (owner_id, is_suspended, ...) are rejected, never ignored.
    model_config = ConfigDict(extra="forbid")

    @field_validator("name", mode="before", check_fields=False)
    @classmethod
    def strip_name(cls, value: Any) -> Any:
        """Trim surrounding whitespace; blank names fail the length check."""
        return value.strip() if isinstance(value, str) else value

    @field_validator("description", "brand_voice", mode="before", check_fields=False)
    @classmethod
    def blank_text_to_none(cls, value: Any) -> Any:
        """Trim free text; empty or whitespace-only text becomes null."""
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("default_language", mode="before", check_fields=False)
    @classmethod
    def validate_language(cls, value: Any) -> Any:
        """Accept short language tags such as en, ur, pt-BR or roman-ur."""
        if value is None:
            return value
        if not isinstance(value, str):
            raise ValueError("default_language must be a string")
        cleaned = value.strip()
        if not LANGUAGE_REGEX.match(cleaned):
            raise ValueError(
                "default_language must be a short language tag such as 'en' or 'ur'"
            )
        return cleaned

    @field_validator("timezone", mode="before", check_fields=False)
    @classmethod
    def validate_timezone(cls, value: Any) -> Any:
        """Require a real IANA timezone name (for example Asia/Karachi)."""
        if value is None:
            return value
        if not isinstance(value, str):
            raise ValueError("timezone must be a string")
        cleaned = value.strip()
        if cleaned == "UTC":
            return cleaned
        try:
            ZoneInfo(cleaned)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise ValueError(
                "timezone must be a valid IANA timezone name such as 'Asia/Karachi'"
            ) from None
        return cleaned

    @field_validator("default_platforms", mode="after", check_fields=False)
    @classmethod
    def dedupe_platforms(cls, value: list[str] | None) -> list[str] | None:
        """Remove duplicate platforms while keeping the given order."""
        if value is None:
            return value
        return list(dict.fromkeys(value))

    @field_validator("settings", mode="after", check_fields=False)
    @classmethod
    def limit_settings_size(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        """Keep the free-form settings object small and JSON serialisable."""
        if value is None:
            return value
        try:
            size = len(json.dumps(value))
        except (TypeError, ValueError):
            raise ValueError("settings must be JSON serialisable") from None
        if size > MAX_SETTINGS_JSON_CHARS:
            raise ValueError(
                f"settings must be at most {MAX_SETTINGS_JSON_CHARS} characters "
                "when serialised"
            )
        return value


class WorkspaceCreate(_WorkspaceFieldValidators):
    """Payload for creating a workspace."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"name": "My Brand"},
                {
                    "name": "Karachi Cafe",
                    "description": "Neighbourhood coffee shop",
                    "brand_voice": "Friendly, short sentences, light humour",
                    "default_language": "ur",
                    "timezone": "Asia/Karachi",
                    "default_platforms": ["instagram", "facebook"],
                    "settings": {"posting_hours": [9, 18]},
                },
            ]
        }
    )

    name: str = Field(..., min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    brand_voice: str | None = Field(default=None, max_length=2000)
    default_language: str = Field(default="en", max_length=10)
    timezone: str = Field(default="UTC", max_length=50)
    default_platforms: list[Platform] | None = None
    settings: dict[str, Any] | None = None


class WorkspaceUpdate(_WorkspaceFieldValidators):
    """Payload for a partial workspace update (only sent fields change).

    Send at least one field. Sending `null` clears the optional fields
    (`description`, `brand_voice`, `default_platforms`, `settings`); `name`,
    `default_language` and `timezone` cannot be null.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"brand_voice": "Warm and playful"},
                {"name": "Karachi Cafe & Bakery", "timezone": "Asia/Karachi"},
                {"default_platforms": ["instagram", "facebook"]},
            ]
        }
    )

    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    brand_voice: str | None = Field(default=None, max_length=2000)
    default_language: str | None = Field(default=None, max_length=10)
    timezone: str | None = Field(default=None, max_length=50)
    default_platforms: list[Platform] | None = None
    settings: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_sent_fields(self) -> "WorkspaceUpdate":
        """Require at least one field and forbid nulling required columns."""
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided")
        for field_name in NON_NULLABLE_UPDATE_FIELDS:
            if (
                field_name in self.model_fields_set
                and getattr(self, field_name) is None
            ):
                raise ValueError(f"{field_name} cannot be null")
        return self


class WorkspaceResponse(BaseModel):
    """Workspace representation returned by the API."""

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={"examples": [WORKSPACE_EXAMPLE]},
    )

    id: UUID
    owner_id: UUID
    name: str
    description: str | None
    brand_voice: str | None
    default_language: str
    timezone: str
    default_platforms: list[str] | None
    settings: dict[str, Any] | None
    is_suspended: bool
    created_at: datetime
    updated_at: datetime


class WorkspaceListResponse(BaseModel):
    """Paginated list of workspaces."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "items": [WORKSPACE_EXAMPLE],
                    "total": 1,
                    "limit": 20,
                    "offset": 0,
                }
            ]
        }
    )

    items: list[WorkspaceResponse]
    total: int
    limit: int
    offset: int
