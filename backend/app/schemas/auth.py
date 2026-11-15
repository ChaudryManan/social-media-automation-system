"""Pydantic schemas for authentication requests and responses."""

import re
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Placeholder values only; none of these are real credentials.
USER_EXAMPLE = {
    "id": "3f1c9a4e-6b1d-4c2a-9a55-0d2f7e8b1c10",
    "email": "owner@example.com",
    "full_name": "Ayesha Khan",
    "role": "user",
    "is_active": True,
    "created_at": "2026-11-14T09:30:00Z",
}


class UserRegisterRequest(BaseModel):
    """Payload for user registration."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "email": "owner@example.com",
                    "password": "Str0ng-Passw0rd!",
                    "full_name": "Ayesha Khan",
                }
            ]
        }
    )

    email: str = Field(..., max_length=255)
    password: str = Field(..., min_length=8, max_length=128)
    full_name: str | None = Field(default=None, max_length=255)

    @field_validator("email")
    @classmethod
    def validate_email_format(cls, value: str) -> str:
        """Validate and normalize email address."""
        cleaned = value.strip().lower()
        if not EMAIL_REGEX.match(cleaned):
            raise ValueError("Invalid email address format")
        return cleaned


class UserLoginRequest(BaseModel):
    """Payload for user login."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{"email": "owner@example.com", "password": "Str0ng-Passw0rd!"}]
        }
    )

    email: str = Field(..., max_length=255)
    password: str = Field(..., max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        """Normalize email address."""
        return value.strip().lower()


class TokenRefreshRequest(BaseModel):
    """Payload for token refresh."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"refresh_token": "example-refresh-token"}]}
    )

    refresh_token: str = Field(..., min_length=1)


class UserLogoutRequest(BaseModel):
    """Payload for logging out a refresh token."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"refresh_token": "example-refresh-token"}]}
    )

    refresh_token: str | None = Field(default=None)


class UserResponse(BaseModel):
    """Public user profile response."""

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={"examples": [USER_EXAMPLE]},
    )

    id: UUID
    email: str
    full_name: str | None
    role: str
    is_active: bool
    created_at: datetime


class TokenResponse(BaseModel):
    """Access and refresh token payload returned upon successful auth."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "access_token": "example-access-token",
                    "refresh_token": "example-refresh-token",
                    "token_type": "bearer",
                    "expires_in": 1800,
                    "user": USER_EXAMPLE,
                }
            ]
        }
    )

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserResponse


class MessageResponse(BaseModel):
    """Generic status message response."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"message": "Successfully logged out"}]}
    )

    message: str


class ForgotPasswordRequest(BaseModel):
    """Payload for initiating a password reset."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"email": "owner@example.com"}]}
    )

    email: str = Field(..., max_length=255)

    @field_validator("email")
    @classmethod
    def validate_email_format(cls, value: str) -> str:
        """Validate and normalize email address."""
        cleaned = value.strip().lower()
        if not EMAIL_REGEX.match(cleaned):
            raise ValueError("Invalid email address format")
        return cleaned


class ResetPasswordRequest(BaseModel):
    """Payload for completing a password reset."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "token": "example-reset-token",
                    "new_password": "An0ther-Str0ng-Passw0rd!",
                }
            ]
        }
    )

    token: str = Field(..., min_length=1, max_length=256)
    new_password: str = Field(..., min_length=8, max_length=128)
