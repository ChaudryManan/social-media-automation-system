"""Integration tests for B1-07 password reset endpoints (requires PostgreSQL)."""

from __future__ import annotations

import socket
from collections.abc import Generator
from unittest.mock import patch
from urllib.parse import urlparse, urlunparse

import psycopg
import pytest
import sqlalchemy as sa
from app.core.config import settings
from app.core.db import get_db
from app.core.security import hash_password, verify_password
from app.main import app
from app.models import Base
from app.models.tokens import RefreshToken
from app.models.users import User
from app.providers.email import BaseEmailSender
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

# ---------------------------------------------------------------------------
# Infrastructure helpers (shared pattern from existing integration tests)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class CapturingEmailSender(BaseEmailSender):
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def send_password_reset(
        self, to_email: str, raw_token: str, reset_base_url: str
    ) -> None:
        self.calls.append({"to_email": to_email, "raw_token": raw_token})


@pytest.fixture()
def db() -> Generator[Session, None, None]:
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


@pytest.fixture()
def email_sender() -> CapturingEmailSender:
    return CapturingEmailSender()


@pytest.fixture()
def client(
    db: Session, email_sender: CapturingEmailSender
) -> Generator[TestClient, None, None]:
    """FastAPI TestClient with overridden DB dependency and mocked rate-limit."""

    def _override_get_db() -> Generator[Session, None, None]:
        yield db

    from app.core import ratelimit as rl_module

    app.dependency_overrides[get_db] = _override_get_db

    # Bypass Redis rate limiting in integration tests (focus on endpoint behaviour)
    with patch.object(rl_module, "check_password_reset_rate_limits", return_value=None):
        with TestClient(app) as test_client:
            yield test_client

    app.dependency_overrides.clear()


@pytest.fixture()
def registered_user(db: Session) -> tuple[User, str]:
    """Create a registered user and return (user, plaintext_password)."""
    plain_pw = "UserPassword123!"
    u = User(
        email="resetuser@example.com",
        password_hash=hash_password(plain_pw),
        role="user",
        is_active=True,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u, plain_pw


# ---------------------------------------------------------------------------
# Helper: directly call service layer with capturing sender (bypassing router)
# ---------------------------------------------------------------------------


def _do_forgot(db: Session, email: str, sender: CapturingEmailSender) -> None:
    from app.services.auth import AuthService

    AuthService.forgot_password(db=db, email=email, email_sender=sender)


def _do_reset(db: Session, raw_token: str, new_password: str) -> None:
    from app.services.auth import AuthService

    AuthService.reset_password(db=db, raw_token=raw_token, new_password=new_password)


# ---------------------------------------------------------------------------
# Integration tests (service layer, real PostgreSQL)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_full_reset_flow(
    db: Session,
    registered_user: tuple[User, str],
    email_sender: CapturingEmailSender,
) -> None:
    """Happy path: forgot → reset → old PW fails → new PW works, login verified."""
    from app.core.errors import UnauthorizedError
    from app.services.auth import AuthService

    user, old_pw = registered_user

    # 1. Login with initial password works and produces a refresh token
    _, _, old_refresh = AuthService.login(db, user.email, old_pw)

    # 2. Request password reset
    _do_forgot(db, user.email, email_sender)
    assert len(email_sender.calls) == 1
    raw_token = email_sender.calls[0]["raw_token"]

    # 3. Reset password
    _do_reset(db, raw_token, "BrandNewPassword789!")
    db.refresh(user)

    # 4. Verify login with new password succeeds
    login_user, new_access, new_refresh = AuthService.login(
        db, user.email, "BrandNewPassword789!"
    )
    assert login_user.id == user.id
    assert new_access is not None
    assert new_refresh is not None

    # 5. Verify login with old password fails
    with pytest.raises(UnauthorizedError) as exc_info:
        AuthService.login(db, user.email, old_pw)
    assert exc_info.value.code == "invalid_credentials"

    # 6. Verify previous refresh token was revoked
    with pytest.raises(UnauthorizedError) as exc_info:
        AuthService.refresh(db, old_refresh)
    assert exc_info.value.code == "invalid_token"


@pytest.mark.integration
def test_complete_flow_with_console_email_sender(
    db: Session,
    registered_user: tuple[User, str],
    capsys: pytest.CaptureFixture,
) -> None:
    """Verify complete reset flow using ConsoleEmailSender stdout token extraction."""
    import re

    from app.core.errors import UnauthorizedError
    from app.providers.email import ConsoleEmailSender
    from app.services.auth import AuthService

    user, old_pw = registered_user
    console_sender = ConsoleEmailSender()

    # 1. User has an active refresh session
    _, _, initial_refresh = AuthService.login(db, user.email, old_pw)

    # 2. Request password reset using ConsoleEmailSender
    AuthService.forgot_password(db=db, email=user.email, email_sender=console_sender)

    # 3. Retrieve raw reset token from ConsoleEmailSender output
    captured = capsys.readouterr()
    match = re.search(r"token=([A-Za-z0-9_-]+)", captured.out)
    assert match is not None
    raw_token = match.group(1)

    # 4. Reset password
    AuthService.reset_password(
        db=db, raw_token=raw_token, new_password="ConsoleResetPassword123!"
    )

    # 5. Verify login with new password succeeds
    logged_in, access_tok, _ = AuthService.login(
        db, user.email, "ConsoleResetPassword123!"
    )
    assert logged_in.email == user.email

    # 6. Verify login with old password fails
    with pytest.raises(UnauthorizedError) as exc_info:
        AuthService.login(db, user.email, old_pw)
    assert exc_info.value.code == "invalid_credentials"

    # 7. Verify previous refresh token is revoked
    with pytest.raises(UnauthorizedError) as exc_info:
        AuthService.refresh(db, initial_refresh)
    assert exc_info.value.code == "invalid_token"

    # 8. Verify the reset token cannot be reused
    with pytest.raises(UnauthorizedError) as exc_info:
        AuthService.reset_password(
            db=db, raw_token=raw_token, new_password="AttemptReusePassword999!"
        )
    assert exc_info.value.code == "token_already_used"


@pytest.mark.integration
def test_reset_token_single_use(
    db: Session,
    registered_user: tuple[User, str],
    email_sender: CapturingEmailSender,
) -> None:
    """Using the same reset token twice raises token_already_used."""
    from app.core.errors import UnauthorizedError

    user, _ = registered_user
    _do_forgot(db, user.email, email_sender)
    raw_token = email_sender.calls[0]["raw_token"]

    _do_reset(db, raw_token, "FirstReset123!")
    with pytest.raises(UnauthorizedError) as exc_info:
        _do_reset(db, raw_token, "SecondReset456!")
    assert exc_info.value.code == "token_already_used"


@pytest.mark.integration
def test_reset_revokes_all_refresh_tokens(
    db: Session,
    registered_user: tuple[User, str],
    email_sender: CapturingEmailSender,
) -> None:
    """All refresh tokens are revoked on successful password reset (PostgreSQL)."""

    user, _ = registered_user
    # Insert two active refresh tokens
    for i in range(2):
        db.add(
            RefreshToken(
                user_id=user.id,
                token_hash=f"refresh_hash_{i}",
                purpose="refresh",
                expires_at=sa.func.now() + sa.text("interval '14 days'"),
            )
        )
    db.commit()

    _do_forgot(db, user.email, email_sender)
    raw_token = email_sender.calls[0]["raw_token"]
    _do_reset(db, raw_token, "ResetPassword123!")

    active_refresh = db.scalars(
        sa.select(RefreshToken).where(
            RefreshToken.user_id == user.id,
            RefreshToken.purpose == "refresh",
            RefreshToken.revoked_at.is_(None),
        )
    ).all()
    assert len(active_refresh) == 0


@pytest.mark.integration
def test_forgot_password_unknown_email_no_token_created(db: Session) -> None:
    """Unknown email: no token is created in the database."""
    sender = CapturingEmailSender()
    _do_forgot(db, "nobody@nowhere.com", sender)
    assert len(sender.calls) == 0
    count = db.scalar(sa.select(sa.func.count()).select_from(RefreshToken))
    assert count == 0


@pytest.mark.integration
def test_second_reset_request_invalidates_first_token(
    db: Session,
    registered_user: tuple[User, str],
    email_sender: CapturingEmailSender,
) -> None:
    """A second forgot-password invalidates the first token."""
    from app.core.errors import UnauthorizedError

    user, _ = registered_user
    _do_forgot(db, user.email, email_sender)
    first_raw = email_sender.calls[0]["raw_token"]

    _do_forgot(db, user.email, email_sender)
    second_raw = email_sender.calls[1]["raw_token"]

    with pytest.raises(UnauthorizedError) as exc_info:
        _do_reset(db, first_raw, "ShouldFail123!")
    assert exc_info.value.code == "token_already_used"

    # Second token still works
    _do_reset(db, second_raw, "ShouldSucceed456!")
    db.refresh(user)
    assert verify_password("ShouldSucceed456!", user.password_hash)


@pytest.mark.integration
def test_reset_password_invalid_token_rejected(
    db: Session, registered_user: tuple[User, str]
) -> None:
    """Completely unknown token returns token_invalid."""
    from app.core.errors import UnauthorizedError

    with pytest.raises(UnauthorizedError) as exc_info:
        _do_reset(db, "totallyfaketoken", "NewPassword123!")
    assert exc_info.value.code == "token_invalid"


@pytest.mark.integration
def test_concurrent_reset_requests_postgresql(
    db: Session,
    registered_user: tuple[User, str],
    email_sender: CapturingEmailSender,
) -> None:
    """Only one of concurrent reset requests with the same token succeeds."""
    import concurrent.futures

    from app.core.errors import UnauthorizedError

    if not _reachable or _TestingSessionLocal is None:
        pytest.skip("PostgreSQL is not reachable; skipping integration test.")

    user, _ = registered_user
    _do_forgot(db, user.email, email_sender)
    raw_token = email_sender.calls[0]["raw_token"]

    results: list[str] = []
    errors: list[str] = []

    def _worker(new_pw: str) -> None:
        sess = _TestingSessionLocal()
        try:
            _do_reset(sess, raw_token, new_pw)
            results.append("success")
        except UnauthorizedError as e:
            errors.append(e.code)
        finally:
            sess.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(_worker, "ConcurrentPass1!")
        f2 = executor.submit(_worker, "ConcurrentPass2!")
        f1.result()
        f2.result()

    assert results == ["success"]
    assert errors == ["token_already_used"]


# ---------------------------------------------------------------------------
# API endpoint integration test (via TestClient)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_forgot_password_api_generic_response(db: Session) -> None:
    """API: forgot-password always returns 200 with generic message."""
    from app.api.v1 import auth as auth_api_mod
    from app.services import auth as auth_svc

    def _override_get_db() -> Generator[Session, None, None]:
        yield db

    app.dependency_overrides[get_db] = _override_get_db

    try:
        with (
            patch.object(auth_api_mod, "check_password_reset_rate_limits"),
            patch.object(
                auth_svc.AuthService,
                "forgot_password",
                side_effect=lambda db, email, email_sender=None: None,
            ),
        ):
            with TestClient(app) as client:
                # Known email (mocked to do nothing)
                res = client.post(
                    "/auth/forgot-password", json={"email": "any@example.com"}
                )
                assert res.status_code == 200
                assert "reset link" in res.json()["message"].lower()

                # Non-existing email — same response
                res2 = client.post(
                    "/auth/forgot-password", json={"email": "ghost@example.com"}
                )
                assert res2.status_code == 200
                assert res2.json()["message"] == res.json()["message"]

                # Dual prefix (/api/v1/auth/forgot-password)
                res_v1 = client.post(
                    "/api/v1/auth/forgot-password", json={"email": "any@example.com"}
                )
                assert res_v1.status_code == 200
                assert res_v1.json()["message"] == res.json()["message"]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.integration
def test_reset_password_api_invalid_token(db: Session) -> None:
    """API: reset-password with bad token returns 401 with token_invalid."""

    def _override_get_db() -> Generator[Session, None, None]:
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as client:
            res = client.post(
                "/auth/reset-password",
                json={"token": "badtoken", "new_password": "NewPass123!"},
            )
            assert res.status_code == 401
            assert res.json()["error"]["code"] == "token_invalid"

            # Dual prefix (/api/v1/auth/reset-password)
            res_v1 = client.post(
                "/api/v1/auth/reset-password",
                json={"token": "badtoken", "new_password": "NewPass123!"},
            )
            assert res_v1.status_code == 401
            assert res_v1.json()["error"]["code"] == "token_invalid"
    finally:
        app.dependency_overrides.clear()
