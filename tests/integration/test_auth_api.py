"""Integration tests for authentication endpoints."""

import socket
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse, urlunparse

import psycopg
import pytest
import sqlalchemy as sa
from app.core.config import settings
from app.core.db import get_db
from app.core.security import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_token,
)
from app.main import app
from app.models import Base
from app.models.tokens import RefreshToken
from app.models.users import User
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


# Setup test engine
_test_url = _get_test_db_url()
_reachable = _postgres_is_reachable(_test_url)

if _reachable:
    # Ensure test database exists
    _parsed = urlparse(_test_url)
    _admin_url = urlunparse(_parsed._replace(scheme="postgresql", path="/postgres"))
    with psycopg.connect(_admin_url, autocommit=True) as _admin_conn:
        _admin_conn.execute("COMMIT")
        _res = _admin_conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = 'sma_test'"
        ).fetchone()
        if not _res:
            _admin_conn.execute("CREATE DATABASE sma_test OWNER sma")

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

    session = _TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        # Clean up database tables
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
def test_register_success(client: TestClient, db: Session) -> None:
    """User registration creates user, returns tokens, and assigns limits."""
    payload = {
        "email": "agent.smith@example.com",
        "password": "Password123!",
        "full_name": "Agent Smith",
    }
    response = client.post("/auth/register", json=payload)
    assert response.status_code == 201

    data = response.json()
    assert "access_token" in data
    assert "refresh_token" in data
    assert data["token_type"] == "bearer"
    assert data["user"]["email"] == "agent.smith@example.com"
    assert data["user"]["full_name"] == "Agent Smith"
    assert data["user"]["role"] == "user"
    assert data["user"]["is_active"] is True

    # Verify DB records
    user = db.scalar(sa.select(User).where(User.email == "agent.smith@example.com"))
    assert user is not None
    assert user.limits is not None
    assert len(user.refresh_tokens) == 1


@pytest.mark.integration
def test_register_duplicate_email(client: TestClient) -> None:
    """Registering with an already registered email should return 409 Conflict."""
    payload = {"email": "duplicate@example.com", "password": "Password123!"}
    res1 = client.post("/auth/register", json=payload)
    assert res1.status_code == 201

    res2 = client.post("/auth/register", json=payload)
    assert res2.status_code == 409
    err = res2.json()["error"]
    assert err["code"] == "user_already_exists"


@pytest.mark.integration
def test_register_case_insensitive_duplicate(client: TestClient) -> None:
    """Registration should be strictly case-insensitive."""
    res1 = client.post(
        "/auth/register",
        json={"email": "CaSe@Example.Com", "password": "Password123!"},
    )
    assert res1.status_code == 201

    res2 = client.post(
        "/auth/register",
        json={"email": "case@example.com", "password": "Password123!"},
    )
    assert res2.status_code == 409
    assert res2.json()["error"]["code"] == "user_already_exists"


@pytest.mark.integration
def test_register_validation_errors(client: TestClient) -> None:
    """Invalid email format or password shorter than 8 chars should fail with 422."""
    res_bad_email = client.post(
        "/auth/register",
        json={"email": "not-an-email", "password": "Password123!"},
    )
    assert res_bad_email.status_code == 422
    assert res_bad_email.json()["error"]["code"] == "validation_error"

    res_short_pw = client.post(
        "/auth/register",
        json={"email": "valid@example.com", "password": "short"},
    )
    assert res_short_pw.status_code == 422
    assert res_short_pw.json()["error"]["code"] == "validation_error"


@pytest.mark.integration
def test_login_success(client: TestClient, db: Session) -> None:
    """Login with valid credentials returns tokens and updates last_login_at."""
    client.post(
        "/auth/register",
        json={"email": "login.test@example.com", "password": "Password123!"},
    )

    res = client.post(
        "/auth/login",
        json={"email": "login.test@example.com", "password": "Password123!"},
    )
    assert res.status_code == 200
    data = res.json()
    assert "access_token" in data
    assert "refresh_token" in data
    assert data["user"]["email"] == "login.test@example.com"

    user = db.scalar(sa.select(User).where(User.email == "login.test@example.com"))
    assert user is not None
    assert user.last_login_at is not None


@pytest.mark.integration
def test_login_invalid_password(client: TestClient) -> None:
    """Login with wrong password should return 401 invalid_credentials."""
    client.post(
        "/auth/register",
        json={"email": "wrong.pw@example.com", "password": "Password123!"},
    )

    res = client.post(
        "/auth/login",
        json={"email": "wrong.pw@example.com", "password": "WrongPassword123!"},
    )
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "invalid_credentials"


@pytest.mark.integration
def test_login_unknown_email(client: TestClient) -> None:
    """Login with non-existent email should return 401 invalid_credentials."""
    res = client.post(
        "/auth/login",
        json={"email": "nonexistent@example.com", "password": "Password123!"},
    )
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "invalid_credentials"


@pytest.mark.integration
def test_login_inactive_user(client: TestClient, db: Session) -> None:
    """Inactive users should be blocked on login with 403 Forbidden."""
    client.post(
        "/auth/register",
        json={"email": "inactive@example.com", "password": "Password123!"},
    )

    # Deactivate user in database
    user = db.scalar(sa.select(User).where(User.email == "inactive@example.com"))
    assert user is not None
    user.is_active = False
    db.commit()

    res = client.post(
        "/auth/login",
        json={"email": "inactive@example.com", "password": "Password123!"},
    )
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "user_inactive"


@pytest.mark.integration
def test_get_me_success(client: TestClient) -> None:
    """GET /auth/me with valid Bearer token should return user profile."""
    reg = client.post(
        "/auth/register",
        json={
            "email": "me@example.com",
            "password": "Password123!",
            "full_name": "My Name",
        },
    )
    token = reg.json()["access_token"]

    res = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    profile = res.json()
    assert profile["email"] == "me@example.com"
    assert profile["full_name"] == "My Name"
    assert profile["role"] == "user"
    assert profile["is_active"] is True


@pytest.mark.integration
def test_get_me_unauthorized_and_invalid(client: TestClient) -> None:
    """GET /auth/me without header or with malformed token should return 401."""
    res_no_auth = client.get("/auth/me")
    assert res_no_auth.status_code == 401
    assert res_no_auth.json()["error"]["code"] == "not_authenticated"

    res_invalid = client.get("/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert res_invalid.status_code == 401
    assert res_invalid.json()["error"]["code"] == "invalid_token"


@pytest.mark.integration
def test_get_me_expired_token(client: TestClient) -> None:
    """GET /auth/me with expired token should return 401 token_expired."""
    expired_token = create_access_token(
        subject="11111111-1111-1111-1111-111111111111",
        email="expired@example.com",
        role="user",
        expires_delta=timedelta(seconds=-1),
    )

    res = client.get("/auth/me", headers={"Authorization": f"Bearer {expired_token}"})
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "token_expired"


@pytest.mark.integration
def test_get_me_inactive_user(client: TestClient, db: Session) -> None:
    """Deactivated user accessing /auth/me should receive 403 user_inactive."""
    reg = client.post(
        "/auth/register",
        json={"email": "will_deactivate@example.com", "password": "Password123!"},
    )
    token = reg.json()["access_token"]

    user = db.scalar(sa.select(User).where(User.email == "will_deactivate@example.com"))
    assert user is not None
    user.is_active = False
    db.commit()

    res = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "user_inactive"


@pytest.mark.integration
def test_refresh_token_rotation(client: TestClient) -> None:
    """Refresh endpoint should rotate refresh token and revoke the old one."""
    reg = client.post(
        "/auth/register",
        json={"email": "rotate@example.com", "password": "Password123!"},
    )
    first_refresh = reg.json()["refresh_token"]

    # Rotate refresh token
    rotate_res = client.post("/auth/refresh", json={"refresh_token": first_refresh})
    assert rotate_res.status_code == 200
    rotate_data = rotate_res.json()
    second_refresh = rotate_data["refresh_token"]
    assert second_refresh != first_refresh
    assert "access_token" in rotate_data

    # Re-using the first refresh token must now be rejected
    reuse_res = client.post("/auth/refresh", json={"refresh_token": first_refresh})
    assert reuse_res.status_code == 401
    assert reuse_res.json()["error"]["code"] == "invalid_token"

    # Using the second refresh token succeeds
    rotate_again_res = client.post(
        "/auth/refresh", json={"refresh_token": second_refresh}
    )
    assert rotate_again_res.status_code == 200


@pytest.mark.integration
def test_refresh_token_expired(client: TestClient, db: Session) -> None:
    """Expired refresh token should return 401 token_expired."""
    user = User(
        email="expired_refresh@example.com",
        password_hash=hash_password("Password123!"),
    )
    db.add(user)
    db.flush()

    raw_token = generate_refresh_token()
    token_row = RefreshToken(
        user_id=user.id,
        token_hash=hash_token(raw_token),
        purpose="refresh",
        expires_at=datetime.now(UTC) - timedelta(hours=1),  # expired
    )
    db.add(token_row)
    db.commit()

    res = client.post("/auth/refresh", json={"refresh_token": raw_token})
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "token_expired"


@pytest.mark.integration
def test_logout_and_revocation(client: TestClient) -> None:
    """Logout should revoke refresh token and succeed idempotently."""
    reg = client.post(
        "/auth/register",
        json={"email": "logout.user@example.com", "password": "Password123!"},
    )
    refresh_token = reg.json()["refresh_token"]

    # Logout
    logout_res = client.post("/auth/logout", json={"refresh_token": refresh_token})
    assert logout_res.status_code == 200
    assert logout_res.json()["message"] == "Successfully logged out"

    # Refreshing with revoked token should fail
    refresh_res = client.post("/auth/refresh", json={"refresh_token": refresh_token})
    assert refresh_res.status_code == 401
    assert refresh_res.json()["error"]["code"] == "invalid_token"

    # Calling logout again is idempotent
    logout_again_res = client.post(
        "/auth/logout", json={"refresh_token": refresh_token}
    )
    assert logout_again_res.status_code == 200


@pytest.mark.integration
def test_dual_prefix_routing(client: TestClient) -> None:
    """Both /auth and /api/v1/auth routes should be accessible."""
    payload = {"email": "dual.prefix@example.com", "password": "Password123!"}
    res_direct = client.post("/auth/register", json=payload)
    assert res_direct.status_code == 201

    res_v1 = client.post(
        "/api/v1/auth/login",
        json={"email": "dual.prefix@example.com", "password": "Password123!"},
    )
    assert res_v1.status_code == 200
    assert "access_token" in res_v1.json()
