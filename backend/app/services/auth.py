"""Authentication business logic service."""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import ConflictError, ForbiddenError, UnauthorizedError
from app.core.security import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_token,
    verify_password,
)
from app.models.limits import UserLimits
from app.models.tokens import RefreshToken
from app.models.users import User

logger = logging.getLogger(__name__)


class AuthService:
    """Authentication and session management operations."""

    @staticmethod
    def register(
        db: Session,
        email: str,
        password: str,
        full_name: str | None = None,
    ) -> tuple[User, str, str]:
        """Register a new user and generate initial tokens.

        Returns (user, access_token, raw_refresh_token).
        """
        clean_email = email.strip().lower()

        existing_user = db.scalar(
            select(User).where(func.lower(User.email) == clean_email)
        )
        if existing_user is not None:
            raise ConflictError(
                message="User with this email already exists",
                code="user_already_exists",
            )

        password_hash = hash_password(password)
        user = User(
            email=clean_email,
            password_hash=password_hash,
            full_name=full_name.strip() if full_name else None,
            role="user",
            is_active=True,
        )
        db.add(user)
        db.flush()  # Populates user.id

        # Associate default user limits
        limits = UserLimits(user_id=user.id)
        db.add(limits)

        # Generate tokens
        access_token = create_access_token(
            subject=str(user.id),
            email=user.email,
            role=user.role,
        )
        raw_refresh_token = generate_refresh_token()
        token_hash = hash_token(raw_refresh_token)

        refresh_row = RefreshToken(
            user_id=user.id,
            token_hash=token_hash,
            purpose="refresh",
            expires_at=datetime.now(UTC) + timedelta(days=settings.REFRESH_TOKEN_DAYS),
        )
        db.add(refresh_row)
        db.commit()
        db.refresh(user)

        logger.info(
            "User registered successfully",
            extra={"user_id": str(user.id), "email": user.email},
        )
        return user, access_token, raw_refresh_token

    @staticmethod
    def login(
        db: Session,
        email: str,
        password: str,
    ) -> tuple[User, str, str]:
        """Authenticate user credentials and issue a new token pair.

        Returns (user, access_token, raw_refresh_token).
        """
        clean_email = email.strip().lower()

        user = db.scalar(select(User).where(func.lower(User.email) == clean_email))
        if user is None or not verify_password(password, user.password_hash):
            raise UnauthorizedError(
                message="Invalid email or password",
                code="invalid_credentials",
            )

        if not user.is_active:
            raise ForbiddenError(
                message="User account is inactive",
                code="user_inactive",
            )

        user.last_login_at = datetime.now(UTC)

        access_token = create_access_token(
            subject=str(user.id),
            email=user.email,
            role=user.role,
        )
        raw_refresh_token = generate_refresh_token()
        token_hash = hash_token(raw_refresh_token)

        refresh_row = RefreshToken(
            user_id=user.id,
            token_hash=token_hash,
            purpose="refresh",
            expires_at=datetime.now(UTC) + timedelta(days=settings.REFRESH_TOKEN_DAYS),
        )
        db.add(refresh_row)
        db.commit()
        db.refresh(user)

        logger.info("User logged in successfully", extra={"user_id": str(user.id)})
        return user, access_token, raw_refresh_token

    @staticmethod
    def refresh(
        db: Session,
        raw_refresh_token: str,
    ) -> tuple[User, str, str]:
        """Validate and rotate a refresh token.

        Revokes the presented refresh token and issues a new token pair.
        Returns (user, new_access_token, new_raw_refresh_token).
        """
        token_hash = hash_token(raw_refresh_token)

        token_row = db.scalar(
            select(RefreshToken).where(
                RefreshToken.token_hash == token_hash,
                RefreshToken.purpose == "refresh",
            )
        )
        if token_row is None or token_row.revoked_at is not None:
            raise UnauthorizedError(
                message="Invalid refresh token",
                code="invalid_token",
            )

        now = datetime.now(UTC)
        if token_row.expires_at <= now:
            raise UnauthorizedError(
                message="Refresh token has expired",
                code="token_expired",
            )

        user = db.get(User, token_row.user_id)
        if user is None:
            raise UnauthorizedError(
                message="User account no longer exists",
                code="user_not_found",
            )

        if not user.is_active:
            raise ForbiddenError(
                message="User account is inactive",
                code="user_inactive",
            )

        # Token rotation: invalidate current token
        token_row.revoked_at = now

        # Issue new token pair
        new_access_token = create_access_token(
            subject=str(user.id),
            email=user.email,
            role=user.role,
        )
        new_raw_refresh_token = generate_refresh_token()
        new_token_hash = hash_token(new_raw_refresh_token)

        new_refresh_row = RefreshToken(
            user_id=user.id,
            token_hash=new_token_hash,
            purpose="refresh",
            expires_at=now + timedelta(days=settings.REFRESH_TOKEN_DAYS),
        )
        db.add(new_refresh_row)
        db.commit()
        db.refresh(user)

        logger.info(
            "Tokens refreshed successfully",
            extra={"user_id": str(user.id)},
        )
        return user, new_access_token, new_raw_refresh_token

    @staticmethod
    def logout(
        db: Session,
        raw_refresh_token: str | None = None,
    ) -> None:
        """Revoke a refresh token on logout (idempotent)."""
        if not raw_refresh_token:
            return

        token_hash = hash_token(raw_refresh_token)
        token_row = db.scalar(
            select(RefreshToken).where(
                RefreshToken.token_hash == token_hash,
                RefreshToken.purpose == "refresh",
            )
        )
        if token_row is not None and token_row.revoked_at is None:
            token_row.revoked_at = datetime.now(UTC)
            db.commit()
            logger.info(
                "Refresh token revoked on logout",
                extra={"user_id": str(token_row.user_id)},
            )
