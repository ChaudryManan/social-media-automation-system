"""Workspace management API router."""

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.api.docs import WORKSPACE_NOT_FOUND, ErrorDoc, errors
from app.core.db import get_db
from app.core.deps import get_current_user, get_workspace_for_user
from app.core.errors import AppError
from app.models.users import User
from app.models.workspaces import Workspace
from app.schemas.workspaces import (
    WorkspaceCreate,
    WorkspaceListResponse,
    WorkspaceResponse,
    WorkspaceUpdate,
)
from app.services.workspaces import WorkspaceService

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


@router.post(
    "",
    response_model=WorkspaceResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a workspace",
    responses=errors(
        401,
        ErrorDoc(
            403,
            "The user is at their workspace limit (`workspace_limit_reached`) "
            "or the account is inactive (`user_inactive`).",
            "workspace_limit_reached",
            "Workspace limit reached: you can have at most 3 workspaces. "
            "Delete one or ask an administrator to raise your limit.",
            {"limit": 3, "current": 3},
        ),
        ErrorDoc(
            409,
            "You already have a workspace with this name (ignoring case).",
            "workspace_name_taken",
            "You already have a workspace with this name",
        ),
        422,
    ),
)
def create_workspace(
    payload: WorkspaceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> WorkspaceResponse:
    """Create a workspace owned by the current user."""
    workspace = WorkspaceService.create(db=db, owner=current_user, data=payload)
    return WorkspaceResponse.model_validate(workspace)


@router.get(
    "",
    response_model=WorkspaceListResponse,
    summary="List my workspaces",
    responses=errors(401, 403, 422),
)
def list_workspaces(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> WorkspaceListResponse:
    """List the current user's workspaces, newest first (deleted ones excluded)."""
    items, total = WorkspaceService.list_for_owner(
        db=db, owner_id=current_user.id, limit=limit, offset=offset
    )
    return WorkspaceListResponse(
        items=[WorkspaceResponse.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{workspace_id}",
    response_model=WorkspaceResponse,
    summary="Get a workspace",
    responses=errors(401, 403, WORKSPACE_NOT_FOUND, 422),
)
def get_workspace(
    workspace: Workspace = Depends(get_workspace_for_user),
) -> WorkspaceResponse:
    """Return one workspace (owner or admin; otherwise 404)."""
    return WorkspaceResponse.model_validate(workspace)


@router.patch(
    "/{workspace_id}",
    response_model=WorkspaceResponse,
    summary="Update a workspace",
    responses=errors(
        401,
        403,
        WORKSPACE_NOT_FOUND,
        ErrorDoc(
            409,
            "Another of your workspaces already has this name (ignoring case).",
            "workspace_name_taken",
            "You already have a workspace with this name",
        ),
        422,
    ),
)
def update_workspace(
    payload: WorkspaceUpdate,
    workspace: Workspace = Depends(get_workspace_for_user),
    db: Session = Depends(get_db),
) -> WorkspaceResponse:
    """Partially update a workspace; only the fields that were sent change."""
    updated = WorkspaceService.update(db=db, workspace=workspace, data=payload)
    return WorkspaceResponse.model_validate(updated)


@router.delete(
    "/{workspace_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete a workspace (soft delete)",
    responses=errors(
        ErrorDoc(
            400,
            "`confirm=true` was not sent (`confirmation_required`).",
            "confirmation_required",
            "Deleting a workspace requires confirm=true",
            {"hint": "Repeat the request with ?confirm=true"},
        ),
        401,
        403,
        WORKSPACE_NOT_FOUND,
        422,
    ),
)
def delete_workspace(
    confirm: bool = Query(
        default=False,
        description="Must be true to confirm the deletion.",
    ),
    workspace: Workspace = Depends(get_workspace_for_user),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    """Soft-delete a workspace. Requires ``?confirm=true``."""
    if not confirm:
        raise AppError(
            code="confirmation_required",
            message="Deleting a workspace requires confirm=true",
            http_status=status.HTTP_400_BAD_REQUEST,
            details={"hint": "Repeat the request with ?confirm=true"},
        )
    WorkspaceService.soft_delete(db=db, workspace=workspace, actor=current_user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
