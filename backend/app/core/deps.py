"""FastAPI request dependencies for authentication and database sessions."""

from uuid import UUID

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import ForbiddenError, NotFoundError, UnauthorizedError
from app.core.security import decode_access_token
from app.models.users import User
from app.models.workspaces import Workspace
from app.services import audit

bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Validate bearer access token and return the current active User."""
    if not credentials or not credentials.credentials:
        raise UnauthorizedError(
            message="Authentication credentials were not provided",
            code="not_authenticated",
        )

    token = credentials.credentials
    try:
        payload = decode_access_token(token)
    except jwt.ExpiredSignatureError:
        raise UnauthorizedError(
            message="Access token has expired",
            code="token_expired",
        )
    except jwt.InvalidTokenError:
        raise UnauthorizedError(
            message="Invalid access token",
            code="invalid_token",
        )

    if payload.get("type") != "access":
        raise UnauthorizedError(
            message="Invalid token type",
            code="invalid_token",
        )

    user_id_str = payload.get("sub")
    if not user_id_str:
        raise UnauthorizedError(
            message="Invalid token payload",
            code="invalid_token",
        )

    try:
        user_id = UUID(user_id_str)
    except (ValueError, TypeError):
        raise UnauthorizedError(
            message="Invalid user identifier in token",
            code="invalid_token",
        )

    user = db.get(User, user_id)
    if not user:
        raise UnauthorizedError(
            message="User account no longer exists",
            code="user_not_found",
        )

    if not user.is_active:
        raise ForbiddenError(
            message="User account is inactive",
            code="user_inactive",
        )

    return user


def require_admin(
    current_user: User = Depends(get_current_user),
) -> User:
    """Ensure the authenticated user holds the platform administrator role."""
    if current_user.role != "admin":
        raise ForbiddenError(
            message="Administrator privileges required",
            code="admin_required",
        )
    return current_user


def get_workspace_for_user(
    request: Request,
    workspace_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Workspace:
    """Return the live workspace if the caller owns it (admins may access any).

    A workspace that does not exist, is soft-deleted, or belongs to someone else
    all produce the same 404, so ids of other users' workspaces are not revealed.

    When an admin reaches a workspace they do not own, the access is written to
    the audit log (``workspace.admin_access``) before the route runs.
    """
    workspace = db.scalar(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.deleted_at.is_(None),
        )
    )
    if workspace is None or (
        workspace.owner_id != current_user.id and current_user.role != "admin"
    ):
        raise NotFoundError(
            message="Workspace not found",
            code="workspace_not_found",
        )

    if workspace.owner_id != current_user.id:
        audit.log(
            db,
            current_user,
            "workspace.admin_access",
            "workspace",
            workspace.id,
            meta={
                "owner_id": workspace.owner_id,
                "method": request.method,
                "path": request.url.path,
            },
            ip_address=request.client.host if request.client else None,
        )
        db.commit()
    return workspace
