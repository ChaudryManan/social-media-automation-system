"""Workflow runs and agent steps (phase B2).

``workflow_runs.request_id`` has no foreign key yet because ``content_requests``
is created in phase B3; that migration adds
``fk_workflow_runs_request_id_content_requests`` (``ON DELETE CASCADE``).

Revision ID: 0002_workflow_runs_agent_steps
Revises: 0001_initial_schema
Create Date: 2026-11-17 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_workflow_runs_agent_steps"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # workflow_runs (DATABASE.md 4.10)
    op.create_table(
        "workflow_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="running",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('running', 'paused', 'completed', 'failed', 'cancelled')",
            name="ck_workflow_runs_run_status",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_workflow_runs"),
    )
    op.create_index(
        "ix_workflow_runs_request_created",
        "workflow_runs",
        ["request_id", sa.text("created_at DESC")],
        unique=False,
    )

    # agent_steps (DATABASE.md 4.11)
    op.create_table(
        "agent_steps",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("node_name", sa.Text(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("verdict", sa.String(length=20), nullable=False),
        sa.Column("feedback", sa.Text(), nullable=True),
        sa.Column(
            "input_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "output_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("tokens_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "verdict IN ('passed', 'failed', 'retried', 'escalated', 'error')",
            name="ck_agent_steps_step_verdict",
        ),
        sa.CheckConstraint("attempt >= 1", name="ck_agent_steps_attempt_positive"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["workflow_runs.id"],
            name="fk_agent_steps_run_id_workflow_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_steps"),
    )
    op.create_index(
        "ix_agent_steps_run_created",
        "agent_steps",
        ["run_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_agent_steps_request_id", "agent_steps", ["request_id"], unique=False
    )
    op.create_index(
        "ix_agent_steps_node_verdict",
        "agent_steps",
        ["node_name", "verdict"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_agent_steps_node_verdict", table_name="agent_steps")
    op.drop_index("ix_agent_steps_request_id", table_name="agent_steps")
    op.drop_index("ix_agent_steps_run_created", table_name="agent_steps")
    op.drop_table("agent_steps")
    op.drop_index("ix_workflow_runs_request_created", table_name="workflow_runs")
    op.drop_table("workflow_runs")
