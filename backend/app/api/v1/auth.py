"""Authentication and session management API router."""

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.orm import Session

from app.api.docs import ErrorDoc, errors
from app.core.config import settings
from app.core.db import get_db
from app.core.deps import get_current_user
from app.core.ratelimit import check_password_reset_rate_limits
from app.models.users import User
from app.schemas.auth import (
    ForgotPasswordRequest,
    MessageResponse,
    ResetPasswordRequest,
    TokenRefreshRequest,
    TokenResponse,
    UserLoginRequest,
    UserLogoutRequest,
    UserRegisterRequest,
    UserResponse,
)
from app.services.auth import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new user account",
    responses=errors(
        ErrorDoc(
            409,
            "An account with this email already exists.",
            "user_already_exists",
            "User with this email already exists",
        ),
        422,
    ),
)
def register(
    payload: UserRegisterRequest,
    db: Session = Depends(get_db),
) -> TokenResponse:
    """Register a new user, automatically grant user role, and return tokens."""
    user, access_token, refresh_token = AuthService.register(
        db=db,
        email=payload.email,
        password=payload.password,
        full_name=payload.full_name,
    )
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=settings.ACCESS_TOKEN_MINUTES * 60,
        user=UserResponse.model_validate(user),
    )


@router.post(
    "/login",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    summary="Authenticate and receive access and refresh tokens",
    responses=errors(
        ErrorDoc(
            401,
            "Wrong email or password (`invalid_credentials`).",
            "invalid_credentials",
            "Invalid email or password",
        ),
        403,
        422,
    ),
)
def login(
    payload: UserLoginRequest,
    db: Session = Depends(get_db),
) -> TokenResponse:
    """Verify email and password, returning a new token pair."""
    user, access_token, refresh_token = AuthService.login(
        db=db,
        email=payload.email,
        password=payload.password,
    )
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=settings.ACCESS_TOKEN_MINUTES * 60,
        user=UserResponse.model_validate(user),
    )


@router.post(
    "/refresh",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    summary="Rotate refresh token and issue new access token",
    responses=errors(
        ErrorDoc(
            401,
            "Refresh token is unknown, revoked or expired "
            "(codes: `invalid_token`, `token_expired`, `user_not_found`).",
            "invalid_token",
            "Invalid refresh token",
        ),
        403,
        422,
    ),
)
def refresh(
    payload: TokenRefreshRequest,
    db: Session = Depends(get_db),
) -> TokenResponse:
    """Rotate an unexpired refresh token, returning a new token pair."""
    user, access_token, refresh_token = AuthService.refresh(
        db=db,
        raw_refresh_token=payload.refresh_token,
    )
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=settings.ACCESS_TOKEN_MINUTES * 60,
        user=UserResponse.model_validate(user),
    )


@router.post(
    "/logout",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Log out and invalidate the active refresh token",
    responses=errors(422),
)
def logout(
    payload: UserLogoutRequest | None = None,
    db: Session = Depends(get_db),
) -> MessageResponse:
    """Revoke the submitted refresh token if provided."""
    raw_token = payload.refresh_token if payload else None
    AuthService.logout(db=db, raw_refresh_token=raw_token)
    return MessageResponse(message="Successfully logged out")


@router.get(
    "/me",
    response_model=UserResponse,
    status_code=status.HTTP_200_OK,
    summary="Get current authenticated user profile",
    responses=errors(401, 403),
)
def get_me(
    current_user: User = Depends(get_current_user),
) -> UserResponse:
    """Return profile details for the authenticated user."""
    return UserResponse.model_validate(current_user)


_FORGOT_PASSWORD_RESPONSE = (
    "If that address is registered, a password reset link has been sent."
)


@router.post(
    "/forgot-password",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Request a password reset link",
    responses=errors(
        422,
        ErrorDoc(
            429,
            "Too many requests from this address or for this account "
            "(codes: `rate_limit_exceeded`, `rate_limit_cooldown`).",
            "rate_limit_exceeded",
            "Too many password reset requests from this address.",
        ),
        ErrorDoc(
            503,
            "The rate limiter (Redis) is unavailable; the request fails closed "
            "(`service_unavailable`).",
            "service_unavailable",
            "Service temporarily unavailable. Please try again later.",
        ),
    ),
)
def forgot_password(
    payload: ForgotPasswordRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> MessageResponse:
    """Send a password reset link if the email is registered.

    Always returns the same generic response regardless of whether the email
    exists in the system (prevents account enumeration).
    Rate-limited per IP and per account.
    """
    client_ip = request.client.host if request.client else "unknown"
    check_password_reset_rate_limits(ip=client_ip, email=payload.email)
    AuthService.forgot_password(db=db, email=payload.email)
    return MessageResponse(message=_FORGOT_PASSWORD_RESPONSE)


@router.post(
    "/reset-password",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Reset password using a valid reset token",
    responses=errors(
        ErrorDoc(
            401,
            "Reset token is invalid, already used or expired "
            "(codes: `token_invalid`, `token_already_used`, `token_expired`).",
            "token_invalid",
            "Invalid password reset token.",
        ),
        422,
    ),
)
def reset_password(
    payload: ResetPasswordRequest,
    db: Session = Depends(get_db),
) -> MessageResponse:
    """Consume a password-reset token and update the account password.

    The token must not be expired, revoked, or already used.
    All active refresh sessions are invalidated on success.
    Note: stateless JWT access tokens issued before this call remain valid
    until their natural expiry (ACCESS_TOKEN_MINUTES).
    """
    AuthService.reset_password(
        db=db,
        raw_token=payload.token,
        new_password=payload.new_password,
    )
    return MessageResponse(message="Password reset successfully. Please log in again.")
