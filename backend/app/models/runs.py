"""Workflow run and agent step models (DATABASE.md 4.10 and 4.11).

A ``WorkflowRun`` is one execution of the generation graph for a content
request; every attempt of every node adds an ``AgentStep`` row to it.

``request_id`` is a plain indexed UUID for now: ``content_requests`` is created
in phase B3, which adds the foreign key (``ON DELETE CASCADE``) in its own
migration.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base

RUN_STATUSES = ("running", "paused", "completed", "failed", "cancelled")
STEP_VERDICTS = ("passed", "failed", "retried", "escalated", "error")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class WorkflowRun(Base):
    """One run of the generation workflow for a request."""

    __tablename__ = "workflow_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    thread_id: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        Enum(
            *RUN_STATUSES,
            name="run_status",
            native_enum=False,
            create_constraint=True,
            length=20,
        ),
        nullable=False,
        default="running",
        server_default="running",
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=_utcnow,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    summary: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB().with_variant(JSON, "sqlite"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=_utcnow,
    )

    steps: Mapped[list["AgentStep"]] = relationship(
        "AgentStep",
        back_populates="run",
        order_by="AgentStep.created_at",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        Index(
            "ix_workflow_runs_request_created",
            request_id,
            created_at.desc(),
        ),
    )


class AgentStep(Base):
    """One attempt of one node inside a run (generation or check)."""

    __tablename__ = "agent_steps"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    node_name: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    attempt: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    verdict: Mapped[str] = mapped_column(
        Enum(
            *STEP_VERDICTS,
            name="step_verdict",
            native_enum=False,
            create_constraint=True,
            length=20,
        ),
        nullable=False,
    )
    feedback: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    input_summary: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB().with_variant(JSON, "sqlite"),
        nullable=True,
    )
    output_summary: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB().with_variant(JSON, "sqlite"),
        nullable=True,
    )
    tokens_in: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    tokens_out: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    cost_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 6),
        nullable=True,
    )
    latency_ms: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    model: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=_utcnow,
    )

    run: Mapped[WorkflowRun] = relationship(
        "WorkflowRun",
        back_populates="steps",
    )

    __table_args__ = (
        CheckConstraint("attempt >= 1", name="attempt_positive"),
        Index("ix_agent_steps_run_created", run_id, created_at),
        Index("ix_agent_steps_request_id", request_id),
        Index("ix_agent_steps_node_verdict", node_name, verdict),
    )
