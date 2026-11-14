"""User account quota and override limits model."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


class UserLimits(Base):
    """Per-user limit overrides defaulting to system_settings values if null."""

    __tablename__ = "user_limits"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    max_workspaces: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    max_accounts_per_workspace: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    monthly_generation_limit: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    user: Mapped["User"] = relationship(  # noqa: F821
        "User",
        back_populates="limits",
    )
