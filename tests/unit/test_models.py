"""Unit tests for SQLAlchemy models and conventions."""

import uuid
from datetime import UTC, datetime, timedelta

from app.core.db import Base
from app.models import (
    AuditLog,
    RefreshToken,
    SystemSettings,
    User,
    UserLimits,
    Workspace,
)


def test_metadata_naming_conventions() -> None:
    """Ensure naming conventions are configured on the declarative base."""
    convention = Base.metadata.naming_convention
    assert convention.get("ix") == "ix_%(column_0_label)s"
    assert convention.get("uq") == "uq_%(table_name)s_%(column_0_name)s"
    assert convention.get("ck") == "ck_%(table_name)s_%(constraint_name)s"
    assert (
        convention.get("fk")
        == "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"
    )
    assert convention.get("pk") == "pk_%(table_name)s"


def test_user_model_defaults_and_email_lowercasing() -> None:
    """User model should lowercase email and assign default values."""
    user = User(
        email="  TestUser@Example.COM  ",
        password_hash="hashed_pw_secret",
    )
    assert user.email == "testuser@example.com"
    assert user.role == "user"
    assert user.is_active is True
    assert isinstance(user.id, uuid.UUID)


def test_refresh_token_defaults() -> None:
    """RefreshToken should have default purpose and app-side UUID."""
    user_id = uuid.uuid4()
    expires = datetime.now(UTC) + timedelta(days=7)
    token = RefreshToken(
        user_id=user_id,
        token_hash="sha256_dummy_hash",
        expires_at=expires,
    )
    assert token.purpose == "refresh"
    assert token.user_id == user_id
    assert isinstance(token.id, uuid.UUID)

    index_names = {idx.name for idx in RefreshToken.__table__.indexes}
    assert "ix_refresh_tokens_user_id" in index_names


def test_workspace_defaults_and_indexes() -> None:
    """Workspace should have language and timezone defaults and partial indexes."""
    owner_id = uuid.uuid4()
    ws = Workspace(
        owner_id=owner_id,
        name="Main Workspace",
    )
    assert ws.default_language == "en"
    assert ws.timezone == "UTC"
    assert ws.is_suspended is False
    assert isinstance(ws.id, uuid.UUID)

    # Inspect index names on Workspace table
    index_names = {idx.name for idx in Workspace.__table__.indexes}
    assert "uq_workspaces_owner_id_name" in index_names
    assert "ix_workspaces_owner_id" in index_names


def test_user_limits_defaults() -> None:
    """UserLimits should instantiate with UUID and null overrides."""
    user_id = uuid.uuid4()
    limits = UserLimits(user_id=user_id)
    assert limits.user_id == user_id
    assert limits.max_workspaces is None
    assert isinstance(limits.id, uuid.UUID)


def test_audit_log_and_system_settings() -> None:
    """AuditLog and SystemSettings models instantiate with expected fields."""
    log = AuditLog(
        action="user.login",
        entity_type="user",
        entity_id="123",
        meta={"ip": "127.0.0.1"},
    )
    assert log.action == "user.login"
    assert isinstance(log.id, uuid.UUID)

    setting = SystemSettings(
        key="max_retries_default",
        value=3,
        description="Default max retry count",
    )
    assert setting.key == "max_retries_default"
    assert setting.value == 3
