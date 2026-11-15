"""Edge cases for access-token checks, reset rate limiting and small helpers (B1-10).

None of these need a database: every case fails before a query is made.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
import redis as redis_lib
from app.core import ratelimit
from app.core.config import settings
from app.core.errors import RateLimitError, ServiceUnavailableError
from app.core.security import JWT_ALGORITHM
from app.main import app
from app.providers.email import ConsoleEmailSender, get_email_sender
from app.schemas.auth import ForgotPasswordRequest
from app.schemas.errors import ErrorResponse
from fastapi.testclient import TestClient
from pydantic import ValidationError

ME = "/api/v1/auth/me"


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def _claims(**overrides: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": "3f1c9a4e-6b1d-4c2a-9a55-0d2f7e8b1c10",
        "email": "u@example.com",
        "role": "user",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        "type": "access",
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


def _bearer(claims: dict[str, Any], key: str | None = None, alg: str = JWT_ALGORITHM):
    token = jwt.encode(claims, key if key is not None else settings.SECRET_KEY, alg)
    return {"Authorization": f"Bearer {token}"}


def _assert_invalid_token(client: TestClient, headers: dict[str, str]) -> None:
    response = client.get(ME, headers=headers)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


# ------------------------------------------------------------ access-token checks


def test_refresh_style_token_is_not_accepted_as_access_token(
    client: TestClient,
) -> None:
    _assert_invalid_token(client, _bearer(_claims(type="refresh")))


def test_token_without_type_claim_is_rejected(client: TestClient) -> None:
    _assert_invalid_token(client, _bearer(_claims(type=None)))


def test_token_with_empty_subject_is_rejected(client: TestClient) -> None:
    _assert_invalid_token(client, _bearer(_claims(sub="")))


def test_token_with_non_uuid_subject_is_rejected(client: TestClient) -> None:
    _assert_invalid_token(client, _bearer(_claims(sub="not-a-uuid")))


@pytest.mark.parametrize("missing", ["sub", "exp", "iat"])
def test_token_missing_a_required_claim_is_rejected(
    client: TestClient, missing: str
) -> None:
    _assert_invalid_token(client, _bearer(_claims(**{missing: None})))


def test_token_signed_with_another_secret_is_rejected(client: TestClient) -> None:
    _assert_invalid_token(client, _bearer(_claims(), key="x" * 40))


def test_unsigned_alg_none_token_is_rejected(client: TestClient) -> None:
    token = jwt.encode(_claims(), key=None, algorithm="none")
    _assert_invalid_token(client, {"Authorization": f"Bearer {token}"})


def test_expired_token_gets_its_own_error_code(client: TestClient) -> None:
    past = int((datetime.now(UTC) - timedelta(minutes=1)).timestamp())

    response = client.get(ME, headers=_bearer(_claims(exp=past)))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "token_expired"


@pytest.mark.parametrize("header", ["Basic abc", "Bearer", "token abc", ""])
def test_malformed_authorization_header_is_unauthenticated(
    client: TestClient, header: str
) -> None:
    response = client.get(ME, headers={"Authorization": header})

    assert response.status_code == 401


# ------------------------------------- password reset rate limiter fails closed


class _RedisDownAtPing:
    def ping(self) -> None:
        raise redis_lib.ConnectionError("down")


class _RedisBreaksMidCheck:
    def ping(self) -> bool:
        return True

    def register_script(self, _script: str):
        def run(**_kwargs: Any) -> int:
            raise redis_lib.TimeoutError("timed out")

        return run


@pytest.mark.parametrize("fake", [_RedisDownAtPing, _RedisBreaksMidCheck])
def test_rate_limiter_fails_closed_when_redis_errors(
    monkeypatch: pytest.MonkeyPatch, fake: type
) -> None:
    monkeypatch.setattr(ratelimit, "_get_redis", lambda: fake())

    with pytest.raises(ServiceUnavailableError) as exc_info:
        ratelimit.check_password_reset_rate_limits(ip="203.0.113.9", email="a@b.co")

    assert exc_info.value.code == "service_unavailable"
    assert exc_info.value.http_status == 503


# ------------------------------------------------------------ small helpers


def test_unknown_email_provider_falls_back_to_console_sender(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "EMAIL_PROVIDER", "carrier-pigeon")

    assert isinstance(get_email_sender(), ConsoleEmailSender)


@pytest.mark.parametrize("bad", ["nope", "a@b", "@b.co", "a b@c.co", ""])
def test_forgot_password_rejects_malformed_email(bad: str) -> None:
    with pytest.raises(ValidationError):
        ForgotPasswordRequest(email=bad)


def test_forgot_password_normalises_email() -> None:
    assert ForgotPasswordRequest(email="  Owner@Example.COM ").email == (
        "owner@example.com"
    )


# ------------------------- documented 429 / 503 reach the client as documented


@pytest.mark.parametrize(
    ("raised", "status", "code"),
    [
        (RateLimitError(message="slow down", code="rate_limit_exceeded"), 429, None),
        (RateLimitError(message="wait", code="rate_limit_cooldown"), 429, None),
        (
            ServiceUnavailableError(message="down", code="service_unavailable"),
            503,
            None,
        ),
    ],
)
def test_forgot_password_rate_limit_errors_use_the_standard_envelope(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    raised: Exception,
    status: int,
    code: str | None,
) -> None:
    def _raise(**_kwargs: Any) -> None:
        raise raised

    monkeypatch.setattr("app.api.v1.auth.check_password_reset_rate_limits", _raise)

    response = client.post(
        "/api/v1/auth/forgot-password", json={"email": "owner@example.com"}
    )

    assert response.status_code == status
    body = ErrorResponse.model_validate(response.json())
    assert body.error.code == raised.code  # type: ignore[attr-defined]
