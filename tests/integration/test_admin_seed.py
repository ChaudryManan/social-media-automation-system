"""Integration tests for RBAC authorization and admin database seeding."""

import socket
from collections.abc import Generator
from urllib.parse import urlparse, urlunparse

import psycopg
import pytest
import sqlalchemy as sa
from app.core.config import settings
from app.core.db import get_db
from app.core.security import create_access_token, hash_password
from app.main import app
from app.models import Base
from app.models.settings import SystemSettings
from app.models.users import User
from app.services.seed import seed_admin, seed_all, seed_system_settings
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker


def _get_test_db_url() -> str:
    url = settings.TEST_DATABASE_URL
    parsed = urlparse(url)
    if parsed.port == 5432 and parsed.hostname in ("localhost", "127.0.0.1", ""):
        raise RuntimeError(
            "Safety error: Refusing to connect to port 5432 on localhost."
        )
    return url


def _postgres_is_reachable(test_db_url: str) -> bool:
    parsed = urlparse(test_db_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 15432
    try:
        sock = socket.create_connection((host, port), timeout=1)
        sock.close()
    except OSError:
        return False

    admin_url = urlunparse(parsed._replace(scheme="postgresql", path="/postgres"))
    try:
        with psycopg.connect(admin_url, connect_timeout=2, autocommit=True) as conn:
            conn.execute("SELECT 1")
            return True
    except (OSError, psycopg.Error):
        return False


def _ensure_test_database() -> None:
    _parsed = urlparse(_test_url)
    _admin_url = urlunparse(_parsed._replace(scheme="postgresql", path="/postgres"))
    with psycopg.connect(_admin_url, autocommit=True) as _admin_conn:
        _admin_conn.execute("COMMIT")
        _res = _admin_conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = 'sma_test'"
        ).fetchone()
        if not _res:
            _admin_conn.execute("CREATE DATABASE sma_test OWNER sma")


_test_url = _get_test_db_url()
_reachable = _postgres_is_reachable(_test_url)

if _reachable:
    _ensure_test_database()
    _test_engine = sa.create_engine(_test_url, poolclass=sa.pool.NullPool)
    _TestingSessionLocal = sessionmaker(
        autocommit=False, autoflush=False, bind=_test_engine
    )
    Base.metadata.create_all(bind=_test_engine)
else:
    _test_engine = None
    _TestingSessionLocal = None


@pytest.fixture
def db() -> Generator[Session, None, None]:
    """Provide an isolated database session and truncate tables after each test."""
    if not _reachable or _test_engine is None or _TestingSessionLocal is None:
        pytest.skip("PostgreSQL is not reachable; skipping integration test.")

    _ensure_test_database()
    Base.metadata.create_all(bind=_test_engine)

    session = _TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        with _test_engine.begin() as conn:
            conn.execute(
                sa.text(
                    "TRUNCATE TABLE users, refresh_tokens, user_limits, "
                    "workspaces, system_settings, audit_logs CASCADE;"
                )
            )


@pytest.fixture
def client(db: Session) -> Generator[TestClient, None, None]:
    """FastAPI TestClient with overridden get_db dependency."""

    def _override_get_db() -> Generator[Session, None, None]:
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.mark.integration
def test_admin_route_authorization(client: TestClient, db: Session) -> None:
    """Admin route should return 200 for admins and 403 for normal users."""
    # 1. Create a normal user and an admin user
    user = User(
        email="regular@example.com",
        password_hash=hash_password("Password123!"),
        role="user",
        is_active=True,
    )
    admin = User(
        email="admin_user@example.com",
        password_hash=hash_password("Password123!"),
        role="admin",
        is_active=True,
    )
    db.add_all([user, admin])
    db.commit()

    user_token = create_access_token(
        subject=str(user.id), email=user.email, role=user.role
    )
    admin_token = create_access_token(
        subject=str(admin.id), email=admin.email, role=admin.role
    )

    # 2. Unauthenticated request -> 401
    res_unauth = client.get("/admin/test")
    assert res_unauth.status_code == 401
    assert res_unauth.json()["error"]["code"] == "not_authenticated"

    # 3. Regular user request -> 403 Forbidden
    res_user = client.get(
        "/admin/test", headers={"Authorization": f"Bearer {user_token}"}
    )
    assert res_user.status_code == 403
    assert res_user.json()["error"]["code"] == "admin_required"

    # 4. Admin request -> 200 OK
    res_admin = client.get(
        "/admin/test", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert res_admin.status_code == 200
    data = res_admin.json()
    assert data["status"] == "ok"
    assert data["role"] == "admin"
    assert data["email"] == "admin_user@example.com"

    # 5. Dual-prefix route (/api/v1/admin/test)
    res_v1 = client.get(
        "/api/v1/admin/test", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert res_v1.status_code == 200


@pytest.mark.integration
def test_seed_admin_missing_credentials_fails(db: Session) -> None:
    """Seeding admin without email or password raises ValueError."""
    with pytest.raises(ValueError, match="SEED_ADMIN_EMAIL is missing or empty"):
        seed_admin(db, email=None, password="SomePassword123!")

    with pytest.raises(ValueError, match="SEED_ADMIN_PASSWORD is missing or empty"):
        seed_admin(db, email="admin@example.com", password=None)


@pytest.mark.integration
def test_seed_admin_prevent_promotion_of_regular_user(db: Session) -> None:
    """Seeding fails if email belongs to regular user; preserves role and password."""
    original_password_hash = hash_password("UserPassword123!")
    user = User(
        email="existing_user@example.com",
        password_hash=original_password_hash,
        role="user",
        is_active=True,
    )
    db.add(user)
    db.commit()

    with pytest.raises(
        ValueError, match="already exists with role 'user'. Automatic role promotion"
    ):
        seed_admin(
            db,
            email="existing_user@example.com",
            password="NewAdminPassword123!",
        )

    # Verify user was not modified
    db.refresh(user)
    assert user.role == "user"
    assert user.password_hash == original_password_hash


@pytest.mark.integration
def test_seed_admin_idempotency(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running seed_admin multiple times should be safe and idempotent."""
    admin_email = "seeded_admin@example.com"
    admin_pass = "SeededAdminPass123!"
    monkeypatch.setattr(settings, "SEED_ADMIN_EMAIL", admin_email)
    monkeypatch.setattr(settings, "SEED_ADMIN_PASSWORD", admin_pass)

    # First seed run creates the admin
    admin1 = seed_admin(db)
    assert admin1 is not None
    assert admin1.role == "admin"
    assert admin1.email == admin_email

    # Second seed run returns existing admin without duplicate rows
    admin2 = seed_admin(db)
    assert admin2.id == admin1.id

    # Verify single row in database
    users = db.scalars(
        sa.select(User).where(User.email == admin_email)
    ).all()
    assert len(users) == 1


@pytest.mark.integration
def test_seed_system_settings_idempotency(db: Session) -> None:
    """seed_system_settings inserts missing defaults without overwriting."""
    # Pre-insert a customized setting
    custom_setting = SystemSettings(
        key="max_retries_default",
        value=10,  # Customized value
        description="Customized limit",
    )
    db.add(custom_setting)
    db.commit()

    # Run seeding
    newly_inserted = seed_system_settings(db)
    # Total canonical is 12; 1 was already present, so 11 newly inserted
    assert newly_inserted == 11

    # Verify customized setting was NOT overwritten
    persisted = db.get(SystemSettings, "max_retries_default")
    assert persisted is not None
    assert persisted.value == 10

    # Second run should insert 0 new settings
    second_run_count = seed_system_settings(db)
    assert second_run_count == 0


@pytest.mark.integration
def test_seed_all_orchestration(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """seed_all should orchestrate both admin user and system settings seeding."""
    admin_email = "orchestrated_admin@example.com"
    admin_pass = "OrchestratedAdminPass123!"
    monkeypatch.setattr(settings, "SEED_ADMIN_EMAIL", admin_email)
    monkeypatch.setattr(settings, "SEED_ADMIN_PASSWORD", admin_pass)

    result = seed_all(db)
    assert result["admin_email"] == admin_email
    assert result["new_settings_count"] == 12

    # Verify admin in DB
    admin = db.scalar(
        sa.select(User).where(User.email == admin_email)
    )
    assert admin is not None
    assert admin.role == "admin"
    assert admin.limits is not None

    # Verify settings in DB
    settings_count = len(db.scalars(sa.select(SystemSettings)).all())
    assert settings_count == 12
