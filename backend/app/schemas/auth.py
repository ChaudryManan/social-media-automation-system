"""Pydantic schemas for authentication requests and responses."""

import re
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class UserRegisterRequest(BaseModel):
    """Payload for user registration."""

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

    email: str = Field(..., max_length=255)
    password: str = Field(..., max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        """Normalize email address."""
        return value.strip().lower()


class TokenRefreshRequest(BaseModel):
    """Payload for token refresh."""

    refresh_token: str = Field(..., min_length=1)


class UserLogoutRequest(BaseModel):
    """Payload for logging out a refresh token."""

    refresh_token: str | None = Field(default=None)


class UserResponse(BaseModel):
    """Public user profile response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    full_name: str | None
    role: str
    is_active: bool
    created_at: datetime


class TokenResponse(BaseModel):
    """Access and refresh token payload returned upon successful auth."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserResponse


class MessageResponse(BaseModel):
    """Generic status message response."""

    message: str


class ForgotPasswordRequest(BaseModel):
    """Payload for initiating a password reset."""

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

    token: str = Field(..., min_length=1, max_length=256)
    new_password: str = Field(..., min_length=8, max_length=128)
