"""Unit tests for cryptographic security and token management."""

import time
from datetime import timedelta

import jwt
import pytest
from app.core.security import (
    create_access_token,
    decode_access_token,
    generate_refresh_token,
    hash_password,
    hash_token,
    verify_password,
)


def test_password_hashing_and_verification() -> None:
    """Argon2 should securely hash passwords and accurately verify them."""
    password = "SuperSecretPassword123!"
    hashed = hash_password(password)

    assert hashed.startswith("$argon2id$")
    assert verify_password(password, hashed) is True
    assert verify_password("WrongPassword!", hashed) is False
    assert verify_password(password, "invalid_hash_string") is False


def test_access_token_creation_and_decoding() -> None:
    """JWT access tokens should embed expected claims and decode correctly."""
    user_id = "11111111-2222-3333-4444-555555555555"
    email = "agent@example.com"
    role = "user"

    token = create_access_token(subject=user_id, email=email, role=role)
    payload = decode_access_token(token)

    assert payload["sub"] == user_id
    assert payload["email"] == email
    assert payload["role"] == role
    assert payload["type"] == "access"
    assert "exp" in payload
    assert "iat" in payload


def test_access_token_expiration() -> None:
    """Expired JWT access tokens should raise ExpiredSignatureError."""
    user_id = "11111111-2222-3333-4444-555555555555"
    token = create_access_token(
        subject=user_id,
        email="test@example.com",
        role="user",
        expires_delta=timedelta(seconds=-1),  # already expired
    )

    with pytest.raises(jwt.ExpiredSignatureError):
        decode_access_token(token)


def test_access_token_invalid_signature() -> None:
    """Tokens signed with a different secret should raise InvalidSignatureError."""
    payload = {
        "sub": "user_id",
        "email": "test@example.com",
        "role": "user",
        "exp": int(time.time()) + 3600,
        "iat": int(time.time()),
        "type": "access",
    }
    tampered_token = jwt.encode(payload, "wrong_secret_key", algorithm="HS256")

    with pytest.raises(jwt.InvalidSignatureError):
        decode_access_token(tampered_token)


def test_refresh_token_generation_and_hashing() -> None:
    """Refresh tokens should be random and produce fixed-length SHA-256 hashes."""
    token1 = generate_refresh_token()
    token2 = generate_refresh_token()

    assert token1 != token2
    assert len(token1) >= 48

    hash1 = hash_token(token1)
    hash2 = hash_token(token2)

    assert len(hash1) == 64
    assert len(hash2) == 64
    assert hash1 != hash2
    assert hash_token(token1) == hash1
