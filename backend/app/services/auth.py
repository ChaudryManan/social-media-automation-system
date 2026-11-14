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
from app.providers.email import BaseEmailSender, get_email_sender

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
        expires_at = token_row.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= now:
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

    # -----------------------------------------------------------------
    # Password reset
    # -----------------------------------------------------------------

    @staticmethod
    def forgot_password(
        db: Session,
        email: str,
        email_sender: BaseEmailSender | None = None,
    ) -> None:
        """Initiate a password-reset flow for the given email address.

        Always returns without revealing whether the account exists.
        The reset token is committed to the database before email dispatch
        so a delivery failure does not roll back the token.

        Args:
            db: Database session.
            email: Normalised (lowercase) email from the request schema.
            email_sender: Injectable email sender (defaults to configured provider).
        """
        if email_sender is None:
            email_sender = get_email_sender()

        clean_email = email.strip().lower()
        now = datetime.now(UTC)

        user = db.scalar(select(User).where(func.lower(User.email) == clean_email))
        if user is None:
            # Return silently — do not reveal account existence.
            logger.info(
                "Password-reset requested for unknown email (no action taken)",
                extra={},  # deliberately no email in log
            )
            return

        # Revoke all existing unused password-reset tokens for this user
        # so only one live reset token exists at a time.
        existing_tokens = db.scalars(
            select(RefreshToken).where(
                RefreshToken.user_id == user.id,
                RefreshToken.purpose == "password_reset",
                RefreshToken.revoked_at.is_(None),
            )
        ).all()
        for tok in existing_tokens:
            tok.revoked_at = now

        # Issue new reset token
        raw_token = generate_refresh_token()
        token_hash = hash_token(raw_token)
        reset_row = RefreshToken(
            user_id=user.id,
            token_hash=token_hash,
            purpose="password_reset",
            expires_at=now + timedelta(minutes=settings.PASSWORD_RESET_MINUTES),
        )
        db.add(reset_row)

        # Commit the token BEFORE attempting email delivery.
        # A delivery failure must not roll back the token — the user may
        # retry and a new token will be issued (the old one still valid if
        # delivery fails silently).
        db.commit()
        logger.info(
            "Password-reset token issued",
            extra={"user_id": str(user.id)},
            # raw_token is NOT logged
        )

        try:
            email_sender.send_password_reset(
                to_email=clean_email,
                raw_token=raw_token,
                reset_base_url=settings.PASSWORD_RESET_BASE_URL,
            )
        except Exception as exc:  # noqa: BLE001
            # Log delivery failure but do not raise — the token is committed
            # and the generic success response is returned to prevent account
            # enumeration via delivery-error messages.
            logger.error(
                "Password-reset email delivery failed",
                extra={"user_id": str(user.id), "error": str(exc)},
                # raw_token intentionally NOT included
            )

    @staticmethod
    def reset_password(
        db: Session,
        raw_token: str,
        new_password: str,
    ) -> None:
        """Consume a password-reset token and update the user's password.

        Uses SELECT … FOR UPDATE to serialise concurrent requests on the same
        token row: only one transaction can consume it; all others see
        revoked_at already set and receive token_already_used.

        On success:
        - The reset token is marked used (revoked_at = now).
        - The user's password_hash is updated.
        - All active refresh tokens for the user are revoked (forces re-login).

        Note: Stateless JWT access tokens issued before the reset remain valid
        until their natural expiry (ACCESS_TOKEN_MINUTES). Shortening that TTL
        or adding a token-revocation store is a future hardening step.

        Raises:
            UnauthorizedError: for invalid / expired / already-used tokens.
        """
        token_hash = hash_token(raw_token)
        now = datetime.now(UTC)

        # Lock the row for the duration of this transaction to prevent
        # concurrent double-consumption.
        token_row = db.scalar(
            select(RefreshToken)
            .where(
                RefreshToken.token_hash == token_hash,
                RefreshToken.purpose == "password_reset",
            )
            .with_for_update()
        )

        if token_row is None:
            raise UnauthorizedError(
                message="Invalid password reset token.",
                code="token_invalid",
            )
        if token_row.revoked_at is not None:
            raise UnauthorizedError(
                message="This password reset token has already been used.",
                code="token_already_used",
            )
        expires_at = token_row.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= now:
            raise UnauthorizedError(
                message="This password reset token has expired.",
                code="token_expired",
            )

        user = db.get(User, token_row.user_id)
        if user is None:
            # Defensive: cascade should prevent this but handle gracefully.
            raise UnauthorizedError(
                message="Invalid password reset token.",
                code="token_invalid",
            )

        # Atomically: consume reset token + update password + revoke refresh tokens
        token_row.revoked_at = now
        user.password_hash = hash_password(new_password)

        # Revoke all active refresh tokens — this forces re-login on all devices.
        # Note: already-issued JWT access tokens remain valid until they expire
        # naturally (they are stateless). This limitation is documented in
        # PROGRESS.md.
        active_refresh = db.scalars(
            select(RefreshToken).where(
                RefreshToken.user_id == user.id,
                RefreshToken.purpose == "refresh",
                RefreshToken.revoked_at.is_(None),
            )
        ).all()
        for tok in active_refresh:
            tok.revoked_at = now

        db.commit()
        logger.info(
            "Password reset completed; all refresh tokens revoked",
            extra={"user_id": str(user.id)},
        )
