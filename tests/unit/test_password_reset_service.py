"""Unit tests for B1-07 password reset service using in-memory SQLite."""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa
from app.core.config import settings
from app.core.errors import RateLimitError, ServiceUnavailableError, UnauthorizedError
from app.core.security import hash_password, hash_token, verify_password
from app.models import Base
from app.models.tokens import RefreshToken
from app.models.users import User
from app.providers.email import BaseEmailSender, ConsoleEmailSender, get_email_sender
from app.services.auth import AuthService
from sqlalchemy.orm import Session, sessionmaker

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class CapturingEmailSender(BaseEmailSender):
    """Test double that captures the raw token and reset link."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def send_password_reset(
        self, to_email: str, raw_token: str, reset_base_url: str
    ) -> None:
        self.calls.append(
            {
                "to_email": to_email,
                "raw_token": raw_token,
                "reset_link": f"{reset_base_url}?token={raw_token}",
            }
        )


@pytest.fixture()
def db() -> Generator[Session, None, None]:
    engine = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def user(db: Session) -> User:
    u = User(
        email="test@example.com",
        password_hash=hash_password("OldPassword123!"),
        role="user",
        is_active=True,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture()
def email_sender() -> CapturingEmailSender:
    return CapturingEmailSender()


# ---------------------------------------------------------------------------
# Email provider tests
# ---------------------------------------------------------------------------


def test_get_email_sender_returns_console_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "EMAIL_PROVIDER", "console")
    sender = get_email_sender()
    assert isinstance(sender, ConsoleEmailSender)


def test_console_email_sender_prints_link(capsys: pytest.CaptureFixture) -> None:
    sender = ConsoleEmailSender()
    sender.send_password_reset(
        to_email="user@example.com",
        raw_token="myrawtoken",
        reset_base_url="http://localhost:3000/reset",
    )
    captured = capsys.readouterr()
    assert "user@example.com" in captured.out
    assert "myrawtoken" in captured.out
    assert "http://localhost:3000/reset?token=myrawtoken" in captured.out


# ---------------------------------------------------------------------------
# forgot_password tests
# ---------------------------------------------------------------------------


def test_forgot_password_unknown_email_returns_silently(
    db: Session, email_sender: CapturingEmailSender
) -> None:
    """Unknown email must not raise and must not send any email."""
    AuthService.forgot_password(
        db=db,
        email="nobody@example.com",
        email_sender=email_sender,
    )
    assert len(email_sender.calls) == 0


def test_forgot_password_creates_reset_token(
    db: Session, user: User, email_sender: CapturingEmailSender
) -> None:
    """A reset token with purpose='password_reset' is committed for a known user."""
    AuthService.forgot_password(db=db, email=user.email, email_sender=email_sender)

    assert len(email_sender.calls) == 1
    raw_token = email_sender.calls[0]["raw_token"]
    expected_hash = hash_token(raw_token)

    token_row = db.scalar(
        sa.select(RefreshToken).where(
            RefreshToken.token_hash == expected_hash,
            RefreshToken.purpose == "password_reset",
        )
    )
    assert token_row is not None
    assert token_row.user_id == user.id
    assert token_row.revoked_at is None
    expires_at = token_row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    assert expires_at > datetime.now(UTC)


def test_forgot_password_revokes_previous_reset_token(
    db: Session, user: User, email_sender: CapturingEmailSender
) -> None:
    """A second reset request invalidates the first token."""
    AuthService.forgot_password(db=db, email=user.email, email_sender=email_sender)
    first_hash = hash_token(email_sender.calls[0]["raw_token"])

    AuthService.forgot_password(db=db, email=user.email, email_sender=email_sender)
    second_hash = hash_token(email_sender.calls[1]["raw_token"])

    # First token is now revoked
    first_row = db.scalar(
        sa.select(RefreshToken).where(RefreshToken.token_hash == first_hash)
    )
    assert first_row is not None
    assert first_row.revoked_at is not None

    # Second token is live
    second_row = db.scalar(
        sa.select(RefreshToken).where(RefreshToken.token_hash == second_hash)
    )
    assert second_row is not None
    assert second_row.revoked_at is None


def test_forgot_password_email_delivery_failure_does_not_rollback_token(
    db: Session, user: User
) -> None:
    """Token is committed even when email delivery raises an exception."""

    class FailingSender(BaseEmailSender):
        def send_password_reset(self, to_email, raw_token, reset_base_url):
            raise RuntimeError("SMTP timeout")

    AuthService.forgot_password(db=db, email=user.email, email_sender=FailingSender())
    # Token should still be in the DB
    token_count = db.scalar(
        sa.select(sa.func.count()).where(
            RefreshToken.user_id == user.id,
            RefreshToken.purpose == "password_reset",
            RefreshToken.revoked_at.is_(None),
        )
    )
    assert token_count == 1


def test_forgot_password_does_not_touch_refresh_tokens(
    db: Session, user: User, email_sender: CapturingEmailSender
) -> None:
    """Existing refresh tokens are not revoked by forgot_password."""
    refresh_tok = RefreshToken(
        user_id=user.id,
        token_hash="dummyhash_refresh",
        purpose="refresh",
        expires_at=datetime.now(UTC) + timedelta(days=14),
    )
    db.add(refresh_tok)
    db.commit()

    AuthService.forgot_password(db=db, email=user.email, email_sender=email_sender)

    db.refresh(refresh_tok)
    assert refresh_tok.revoked_at is None


# ---------------------------------------------------------------------------
# reset_password tests
# ---------------------------------------------------------------------------


def _create_reset_token(
    db: Session,
    user: User,
    *,
    minutes: int = 60,
    already_used: bool = False,
) -> str:
    """Helper: create a password_reset token and return the raw token string."""
    from app.core.security import generate_refresh_token

    raw = generate_refresh_token()
    tok = RefreshToken(
        user_id=user.id,
        token_hash=hash_token(raw),
        purpose="password_reset",
        expires_at=datetime.now(UTC) + timedelta(minutes=minutes),
        revoked_at=datetime.now(UTC) if already_used else None,
    )
    db.add(tok)
    db.commit()
    return raw


def test_reset_password_success(
    db: Session, user: User, email_sender: CapturingEmailSender
) -> None:
    """Happy path: password is updated and can be verified."""
    raw = _create_reset_token(db, user)
    AuthService.reset_password(db=db, raw_token=raw, new_password="NewPassword456!")
    db.refresh(user)
    assert verify_password("NewPassword456!", user.password_hash)


def test_reset_password_old_password_rejected(db: Session, user: User) -> None:
    """After a reset, the old password must not authenticate."""
    raw = _create_reset_token(db, user)
    AuthService.reset_password(db=db, raw_token=raw, new_password="NewPassword456!")
    db.refresh(user)
    assert not verify_password("OldPassword123!", user.password_hash)


def test_reset_password_revokes_refresh_tokens(db: Session, user: User) -> None:
    """All refresh tokens are revoked on successful password reset."""
    refresh_tok = RefreshToken(
        user_id=user.id,
        token_hash="dummyhash_ref",
        purpose="refresh",
        expires_at=datetime.now(UTC) + timedelta(days=14),
    )
    db.add(refresh_tok)
    db.commit()

    raw = _create_reset_token(db, user)
    AuthService.reset_password(db=db, raw_token=raw, new_password="NewPassword456!")

    db.refresh(refresh_tok)
    assert refresh_tok.revoked_at is not None


def test_reset_password_consumes_token(db: Session, user: User) -> None:
    """The reset token's revoked_at is set after use."""
    raw = _create_reset_token(db, user)
    token_hash = hash_token(raw)
    AuthService.reset_password(db=db, raw_token=raw, new_password="NewPassword456!")

    row = db.scalar(
        sa.select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    )
    assert row is not None
    assert row.revoked_at is not None


def test_reset_password_token_already_used(db: Session, user: User) -> None:
    """A used token is rejected with token_already_used."""
    raw = _create_reset_token(db, user, already_used=True)
    with pytest.raises(UnauthorizedError) as exc_info:
        AuthService.reset_password(db=db, raw_token=raw, new_password="NewPassword456!")
    assert exc_info.value.code == "token_already_used"


def test_reset_password_second_call_rejected_after_consumption(
    db: Session, user: User
) -> None:
    """A token consumed by one request cannot be consumed by a second request."""
    raw = _create_reset_token(db, user)
    AuthService.reset_password(db=db, raw_token=raw, new_password="FirstNewPass123!")
    with pytest.raises(UnauthorizedError) as exc_info:
        AuthService.reset_password(
            db=db, raw_token=raw, new_password="SecondNewPass456!"
        )
    assert exc_info.value.code == "token_already_used"


def test_reset_password_token_expired(db: Session, user: User) -> None:
    """An expired token is rejected with token_expired."""
    raw = _create_reset_token(db, user, minutes=-1)
    with pytest.raises(UnauthorizedError) as exc_info:
        AuthService.reset_password(db=db, raw_token=raw, new_password="NewPassword456!")
    assert exc_info.value.code == "token_expired"


def test_reset_password_token_invalid(db: Session, user: User) -> None:
    """A completely unknown token is rejected with token_invalid."""
    with pytest.raises(UnauthorizedError) as exc_info:
        AuthService.reset_password(
            db=db, raw_token="completelyfaketoken", new_password="NewPassword456!"
        )
    assert exc_info.value.code == "token_invalid"


def test_reset_password_does_not_consume_refresh_token(db: Session, user: User) -> None:
    """A raw refresh token cannot be used as a reset token."""
    refresh_raw = "somerawrefreshtoken"
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_token(refresh_raw),
            purpose="refresh",
            expires_at=datetime.now(UTC) + timedelta(days=14),
        )
    )
    db.commit()
    with pytest.raises(UnauthorizedError) as exc_info:
        AuthService.reset_password(
            db=db, raw_token=refresh_raw, new_password="NewPassword456!"
        )
    assert exc_info.value.code == "token_invalid"


def test_multiple_reset_requests_only_latest_token_valid(
    db: Session, user: User, email_sender: CapturingEmailSender
) -> None:
    """Only the most recent reset token is usable; previous ones are revoked."""
    AuthService.forgot_password(db=db, email=user.email, email_sender=email_sender)
    first_raw = email_sender.calls[0]["raw_token"]

    AuthService.forgot_password(db=db, email=user.email, email_sender=email_sender)
    second_raw = email_sender.calls[1]["raw_token"]

    with pytest.raises(UnauthorizedError) as exc_info:
        AuthService.reset_password(
            db=db, raw_token=first_raw, new_password="NewPassword456!"
        )
    assert exc_info.value.code == "token_already_used"

    # Second token still works
    AuthService.reset_password(
        db=db, raw_token=second_raw, new_password="NewPassword456!"
    )
    db.refresh(user)
    assert verify_password("NewPassword456!", user.password_hash)


# ---------------------------------------------------------------------------
# Rate limiting tests (mocked Redis)
# ---------------------------------------------------------------------------


def _make_redis_mock(*, incr_values: list[int], nx_result: str | None = "OK"):
    """Return a mock Redis client controlling INCR and SET NX results."""
    mock = MagicMock()
    # register_script returns a callable script; we control its return values
    incr_iter = iter(incr_values)
    incr_script = MagicMock(side_effect=lambda keys, args: next(incr_iter))
    nx_script = MagicMock(return_value=nx_result)
    mock.register_script.side_effect = [incr_script, nx_script, incr_script]
    return mock, incr_script, nx_script


def _patched_rate_limit(
    *,
    ip: str = "1.2.3.4",
    email: str = "user@example.com",
    redis_mock=None,
) -> None:
    from app.core import ratelimit as rl_module

    with patch.object(rl_module, "_get_redis", return_value=redis_mock):
        from app.core.ratelimit import check_password_reset_rate_limits

        check_password_reset_rate_limits(ip=ip, email=email)


def test_rate_limit_ip_exceeded(monkeypatch: pytest.MonkeyPatch) -> None:
    """IP limit: 6th request (> PW_RESET_IP_LIMIT=5) raises RateLimitError."""
    monkeypatch.setattr(settings, "PW_RESET_IP_LIMIT", 5)

    from app.core import ratelimit as rl_module

    mock_redis = MagicMock()
    # First register_script call → incr script returning 6 (over limit)
    incr_script = MagicMock(return_value=6)
    nx_script = MagicMock(return_value="OK")
    mock_redis.register_script.side_effect = [incr_script, nx_script, incr_script]

    with patch.object(rl_module, "_get_redis", return_value=mock_redis):
        with pytest.raises(RateLimitError) as exc_info:
            rl_module.check_password_reset_rate_limits(ip="1.2.3.4", email="a@b.com")
    assert exc_info.value.code == "rate_limit_exceeded"


def test_rate_limit_account_cooldown_active(monkeypatch: pytest.MonkeyPatch) -> None:
    """Account cooldown: SET NX returns None → cooldown active → RateLimitError."""
    monkeypatch.setattr(settings, "PW_RESET_IP_LIMIT", 5)

    from app.core import ratelimit as rl_module

    mock_redis = MagicMock()
    incr_script = MagicMock(return_value=1)  # IP count = 1 (under limit)
    nx_script = MagicMock(return_value=None)  # Key already existed → cooldown
    mock_redis.register_script.side_effect = [incr_script, nx_script, incr_script]

    with patch.object(rl_module, "_get_redis", return_value=mock_redis):
        with pytest.raises(RateLimitError) as exc_info:
            rl_module.check_password_reset_rate_limits(ip="1.2.3.4", email="a@b.com")
    assert exc_info.value.code == "rate_limit_cooldown"


def test_rate_limit_account_window_exceeded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Account window: 4th account request (> PW_RESET_ACCT_LIMIT=3) raises error."""
    monkeypatch.setattr(settings, "PW_RESET_IP_LIMIT", 5)
    monkeypatch.setattr(settings, "PW_RESET_ACCT_LIMIT", 3)

    from app.core import ratelimit as rl_module

    mock_redis = MagicMock()
    # incr_script called twice: once for IP (returns 1), once for account (returns 4)
    incr_script = MagicMock(side_effect=[1, 4])
    nx_script = MagicMock(return_value="OK")  # Cooldown not active
    mock_redis.register_script.side_effect = [incr_script, nx_script, incr_script]

    with patch.object(rl_module, "_get_redis", return_value=mock_redis):
        with pytest.raises(RateLimitError) as exc_info:
            rl_module.check_password_reset_rate_limits(ip="1.2.3.4", email="a@b.com")
    assert exc_info.value.code == "rate_limit_exceeded"


def test_rate_limit_redis_unavailable_raises_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redis unreachable → ServiceUnavailableError (fail-closed)."""
    import redis as redis_lib
    from app.core import ratelimit as rl_module

    failing_redis = MagicMock()
    failing_redis.ping.side_effect = redis_lib.ConnectionError("refused")

    with patch.object(rl_module, "_get_redis", return_value=failing_redis):
        with pytest.raises(ServiceUnavailableError) as exc_info:
            rl_module.check_password_reset_rate_limits(ip="1.2.3.4", email="a@b.com")
    assert exc_info.value.http_status == 503
