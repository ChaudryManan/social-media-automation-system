"""Per-user limits and quota checks (PRD FR-2a.4, DATABASE.md rule 8).

A user's effective limit is the ``user_limits`` override when it is set, else
the ``system_settings`` default, else the code fallback below (DATABASE.md
4.17: "defaults live in code"). A limit of ``0`` is valid and blocks creation.
"""

import logging
import uuid
from dataclasses import dataclass

from fastapi import status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import LimitExceededError
from app.models.limits import UserLimits
from app.models.settings import SystemSettings
from app.models.workspaces import Workspace

logger = logging.getLogger(__name__)

# Code fallbacks, used only when the system_settings row is missing or invalid.
# They mirror the seeded values in ``services/seed.py``.
FALLBACK_DEFAULTS: dict[str, int] = {
    "default_max_workspaces": 3,
    "default_max_accounts_per_workspace": 5,
    "default_monthly_generation_limit": 100,
}


@dataclass(frozen=True)
class EffectiveLimits:
    """The limits that apply to one user right now."""

    max_workspaces: int
    max_accounts_per_workspace: int
    monthly_generation_limit: int


def _is_valid_limit(value: object) -> bool:
    """A limit is a non-negative integer (``bool`` is not accepted)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _system_default(db: Session, key: str) -> int:
    """Return the system-wide default for ``key`` (settings row, else fallback)."""
    row = db.get(SystemSettings, key)
    if row is not None:
        if _is_valid_limit(row.value):
            return int(row.value)  # type: ignore[arg-type]
        logger.warning(
            "Invalid system setting for limit; using code fallback",
            extra={"setting_key": key},
        )
    return FALLBACK_DEFAULTS[key]


def _resolve(db: Session, override: int | None, default_key: str) -> int:
    if override is not None and _is_valid_limit(override):
        return override
    return _system_default(db, default_key)


def get_effective_limits(db: Session, user_id: uuid.UUID) -> EffectiveLimits:
    """Return the limits for ``user_id`` (override first, then defaults)."""
    row = db.scalar(select(UserLimits).where(UserLimits.user_id == user_id))
    return EffectiveLimits(
        max_workspaces=_resolve(
            db, row.max_workspaces if row else None, "default_max_workspaces"
        ),
        max_accounts_per_workspace=_resolve(
            db,
            row.max_accounts_per_workspace if row else None,
            "default_max_accounts_per_workspace",
        ),
        monthly_generation_limit=_resolve(
            db,
            row.monthly_generation_limit if row else None,
            "default_monthly_generation_limit",
        ),
    )


def count_workspaces(db: Session, owner_id: uuid.UUID) -> int:
    """Count the owner's live (not soft-deleted) workspaces."""
    total = db.scalar(
        select(func.count())
        .select_from(Workspace)
        .where(Workspace.owner_id == owner_id, Workspace.deleted_at.is_(None))
    )
    return int(total or 0)


def ensure_can_create_workspace(db: Session, user_id: uuid.UUID) -> None:
    """Raise ``LimitExceededError`` (403) if the user is at ``max_workspaces``.

    Soft-deleted workspaces do not count. Callers should hold a lock on the
    user row so two concurrent creates cannot both pass the check.
    """
    limit = get_effective_limits(db, user_id).max_workspaces
    current = count_workspaces(db, user_id)
    if current >= limit:
        raise LimitExceededError(
            message=(
                f"Workspace limit reached: you can have at most {limit} "
                f"workspace{'s' if limit != 1 else ''}. Delete one or ask an "
                "administrator to raise your limit."
            ),
            code="workspace_limit_reached",
            http_status=status.HTTP_403_FORBIDDEN,
            details={"limit": limit, "current": current},
        )
