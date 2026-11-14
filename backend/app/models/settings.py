"""System settings key-value entity model."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


class SystemSettings(Base):
    """Dynamic key-value application settings stored in the database."""

    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(
        String(100),
        primary_key=True,
    )
    value: Mapped[dict[str, Any] | list[Any] | str | int | float | bool] = (
        mapped_column(
            JSONB().with_variant(JSON, "sqlite"),
            nullable=False,
        )
    )
    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    modifier: Mapped["User | None"] = relationship(  # noqa: F821
        "User",
        foreign_keys=[updated_by],
    )
