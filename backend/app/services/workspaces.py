"""Workspace business logic service."""

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ConflictError
from app.models.users import User
from app.models.workspaces import Workspace
from app.schemas.workspaces import WorkspaceCreate, WorkspaceUpdate

logger = logging.getLogger(__name__)


def _name_conflict() -> ConflictError:
    return ConflictError(
        message="You already have a workspace with this name",
        code="workspace_name_taken",
    )


class WorkspaceService:
    """Create, read, update and soft-delete workspaces.

    Authorization (owner or admin) is enforced by the ``get_workspace_for_user``
    dependency in ``app.core.deps``; methods here assume the caller is allowed.
    """

    @staticmethod
    def _name_taken(
        db: Session,
        owner_id: uuid.UUID,
        name: str,
        exclude_id: uuid.UUID | None = None,
    ) -> bool:
        """Return whether the owner has a live workspace with this name.

        The comparison ignores case, so "Brand" and "brand" count as the same
        name. This is stricter than the database's partial unique index, which
        remains the final guard against concurrent requests.
        """
        stmt = select(Workspace.id).where(
            Workspace.owner_id == owner_id,
            Workspace.deleted_at.is_(None),
            func.lower(Workspace.name) == func.lower(name),
        )
        if exclude_id is not None:
            stmt = stmt.where(Workspace.id != exclude_id)
        return db.scalar(stmt.limit(1)) is not None

    @staticmethod
    def create(db: Session, owner: User, data: WorkspaceCreate) -> Workspace:
        """Create a workspace owned by ``owner``."""
        if WorkspaceService._name_taken(db, owner.id, data.name):
            raise _name_conflict()

        workspace = Workspace(
            owner_id=owner.id,
            name=data.name,
            description=data.description,
            brand_voice=data.brand_voice,
            default_language=data.default_language,
            timezone=data.timezone,
            default_platforms=data.default_platforms,
            settings=data.settings,
        )
        db.add(workspace)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise _name_conflict() from None
        db.refresh(workspace)

        logger.info(
            "Workspace created",
            extra={"workspace_id": str(workspace.id), "user_id": str(owner.id)},
        )
        return workspace

    @staticmethod
    def list_for_owner(
        db: Session,
        owner_id: uuid.UUID,
        limit: int,
        offset: int,
    ) -> tuple[list[Workspace], int]:
        """Return one page of the owner's live workspaces and the total count."""
        conditions = (
            Workspace.owner_id == owner_id,
            Workspace.deleted_at.is_(None),
        )
        total = db.scalar(
            select(func.count()).select_from(Workspace).where(*conditions)
        )
        items = db.scalars(
            select(Workspace)
            .where(*conditions)
            .order_by(Workspace.created_at.desc(), Workspace.id.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        return list(items), int(total or 0)

    @staticmethod
    def update(db: Session, workspace: Workspace, data: WorkspaceUpdate) -> Workspace:
        """Apply the fields the client sent; omitted fields stay unchanged."""
        changes = data.model_dump(exclude_unset=True)

        new_name = changes.get("name")
        if new_name is not None and WorkspaceService._name_taken(
            db, workspace.owner_id, new_name, exclude_id=workspace.id
        ):
            raise _name_conflict()

        for field_name, value in changes.items():
            setattr(workspace, field_name, value)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise _name_conflict() from None
        db.refresh(workspace)

        logger.info(
            "Workspace updated",
            extra={
                "workspace_id": str(workspace.id),
                "fields": sorted(changes.keys()),
            },
        )
        return workspace

    @staticmethod
    def soft_delete(db: Session, workspace: Workspace) -> None:
        """Soft-delete a workspace (it disappears from every list and lookup).

        When pipeline tables exist (B3), this must also cancel the workspace's
        pending scheduled jobs and non-final requests (DATABASE.md rule 7).
        """
        workspace.deleted_at = datetime.now(UTC)
        db.commit()

        logger.info(
            "Workspace deleted",
            extra={"workspace_id": str(workspace.id)},
        )
