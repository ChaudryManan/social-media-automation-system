"""Unit tests for database seeding and security rules using SQLite in-memory."""

import pytest
import sqlalchemy as sa
from app.core.config import settings
from app.core.security import hash_password, verify_password
from app.models import Base
from app.models.settings import SystemSettings
from app.models.users import User
from app.services.seed import seed_admin, seed_all, seed_system_settings
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture
def sqlite_session() -> Session:
    """Create a fresh in-memory SQLite database and session for unit testing."""
    engine = sa.create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_seed_admin_missing_email_fails(
    sqlite_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """seed_admin must raise ValueError if admin email is missing or empty."""
    monkeypatch.setattr(settings, "SEED_ADMIN_EMAIL", None)
    monkeypatch.setattr(settings, "SEED_ADMIN_PASSWORD", "SuperSecurePassword123!")

    with pytest.raises(ValueError, match="SEED_ADMIN_EMAIL is missing or empty"):
        seed_admin(sqlite_session, email=None)

    with pytest.raises(ValueError, match="SEED_ADMIN_EMAIL is missing or empty"):
        seed_admin(sqlite_session, email="   ")


def test_seed_admin_missing_password_fails(
    sqlite_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """seed_admin must raise ValueError if admin password is missing or empty."""
    monkeypatch.setattr(settings, "SEED_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setattr(settings, "SEED_ADMIN_PASSWORD", None)

    with pytest.raises(ValueError, match="SEED_ADMIN_PASSWORD is missing or empty"):
        seed_admin(sqlite_session, email="admin@example.com", password=None)

    with pytest.raises(ValueError, match="SEED_ADMIN_PASSWORD is missing or empty"):
        seed_admin(sqlite_session, email="admin@example.com", password="   ")


def test_seed_admin_new_account_creation_works(
    sqlite_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """seed_admin should successfully create a new administrator account."""
    monkeypatch.setattr(settings, "SEED_ADMIN_EMAIL", "newadmin@example.com")
    monkeypatch.setattr(settings, "SEED_ADMIN_PASSWORD", "SuperAdminPassword123!")

    admin = seed_admin(sqlite_session)
    assert admin.id is not None
    assert admin.email == "newadmin@example.com"
    assert admin.role == "admin"
    assert admin.is_active is True
    assert verify_password("SuperAdminPassword123!", admin.password_hash)

    # Verify limits were created
    assert admin.limits is not None
    assert admin.limits.max_workspaces == 100
    assert admin.limits.monthly_generation_limit == 10000


def test_seed_admin_existing_admin_account_no_duplicate(
    sqlite_session: Session,
) -> None:
    """Existing admin account should remain unchanged without duplicate creation."""
    original_admin = seed_admin(
        sqlite_session,
        email="existing_admin@example.com",
        password="FirstPassword123!",
    )
    original_id = original_admin.id
    original_hash = original_admin.password_hash

    # Run seed_admin again with different password; should leave account untouched
    second_admin = seed_admin(
        sqlite_session,
        email="existing_admin@example.com",
        password="DifferentPassword456!",
    )
    assert second_admin.id == original_id
    assert second_admin.password_hash == original_hash
    assert verify_password("FirstPassword123!", second_admin.password_hash)

    # Ensure only 1 record exists in DB
    users = sqlite_session.scalars(
        sa.select(User).where(User.email == "existing_admin@example.com")
    ).all()
    assert len(users) == 1


def test_seed_admin_existing_regular_user_fails_and_preserves_role(
    sqlite_session: Session,
) -> None:
    """Seeding fails if email belongs to regular user; preserves role and password."""
    original_password_hash = hash_password("RegularPassword123!")
    regular_user = User(
        email="regular_user@example.com",
        password_hash=original_password_hash,
        role="user",
        is_active=True,
    )
    sqlite_session.add(regular_user)
    sqlite_session.commit()
    sqlite_session.refresh(regular_user)
    user_id = regular_user.id

    # Attempting to seed admin with regular user's email must fail
    with pytest.raises(
        ValueError,
        match="already exists with role 'user'. Automatic role promotion",
    ):
        seed_admin(
            sqlite_session,
            email="regular_user@example.com",
            password="AttemptedAdminPassword123!",
        )

    # Verify user record is unmodified
    persisted_user = sqlite_session.get(User, user_id)
    assert persisted_user is not None
    assert persisted_user.role == "user"
    assert persisted_user.password_hash == original_password_hash
    assert verify_password("RegularPassword123!", persisted_user.password_hash)


def test_seed_system_settings_preserves_existing_settings(
    sqlite_session: Session,
) -> None:
    """Ensure seed_system_settings preserves pre-existing settings."""
    # Pre-insert customized setting
    custom = SystemSettings(
        key="max_retries_default",
        value=99,
        description="Custom admin retry limit",
    )
    sqlite_session.add(custom)
    sqlite_session.commit()

    # Seed defaults
    inserted = seed_system_settings(sqlite_session)
    assert inserted == 11

    # Verify existing setting was preserved
    setting = sqlite_session.get(SystemSettings, "max_retries_default")
    assert setting is not None
    assert setting.value == 99
    assert setting.description == "Custom admin retry limit"


def test_seed_all_orchestration(
    sqlite_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """seed_all orchestrates admin and settings seeding."""
    monkeypatch.setattr(settings, "SEED_ADMIN_EMAIL", "platform_admin@example.com")
    monkeypatch.setattr(settings, "SEED_ADMIN_PASSWORD", "PlatformPassword123!")

    result = seed_all(sqlite_session)
    assert result["admin_email"] == "platform_admin@example.com"
    assert result["new_settings_count"] == 12

    admin = sqlite_session.scalar(
        sa.select(User).where(User.email == "platform_admin@example.com")
    )
    assert admin is not None
    assert admin.role == "admin"
