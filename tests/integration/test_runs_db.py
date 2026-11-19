"""Workflow runs and agent steps on real PostgreSQL, built by migration 0002."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import sqlalchemy as sa
from app.models import AgentStep, WorkflowRun
from app.services import runs
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration


def test_run_with_steps_round_trips_through_postgres(
    pg: Session, migrated_engine: sa.Engine
) -> None:
    request_id = uuid.uuid4()
    run = runs.start_run(pg, request_id)
    runs.record_step(
        pg,
        run,
        "writer",
        1,
        "failed",
        feedback="too long",
        input_summary={"topic": "habits", "tags": ["a", "b"]},
        output_summary={"chars": 412},
        tokens_in=100,
        tokens_out=50,
        cost_usd=0.0012345678,
        latency_ms=420,
        model="m-1",
    )
    runs.record_step(pg, run, "writer", 2, "passed", cost_usd=0.25)
    pg.commit()
    run_id = run.id

    with Session(migrated_engine) as fresh:  # a different connection
        loaded = runs.get_run(fresh, run_id)
        steps = runs.list_steps(fresh, run_id)

        assert loaded.request_id == request_id
        assert loaded.thread_id == str(request_id)
        assert loaded.status == "running"
        assert loaded.started_at.tzinfo is not None
        assert [(s.attempt, s.verdict) for s in steps] == [(1, "failed"), (2, "passed")]
        first = steps[0]
        assert first.input_summary == {"topic": "habits", "tags": ["a", "b"]}
        assert first.output_summary == {"chars": 412}
        assert first.cost_usd == Decimal("0.001235")
        assert (first.tokens_in, first.tokens_out, first.latency_ms) == (100, 50, 420)
        assert first.request_id == request_id
        assert steps[1].cost_usd == Decimal("0.250000")
        assert steps[1].tokens_in == 0  # server/ORM default


def test_run_lifecycle_and_summary_on_postgres(pg: Session) -> None:
    run = runs.start_run(pg, uuid.uuid4())
    runs.record_step(pg, run, "writer", 1, "retried", tokens_in=4, cost_usd=0.1)
    runs.record_step(pg, run, "writer", 2, "passed", tokens_out=6, cost_usd=0.2)
    runs.set_run_status(pg, run, "paused")
    runs.set_run_status(pg, run, "running")
    runs.set_run_status(pg, run, "completed")
    pg.commit()
    pg.expire_all()

    reloaded = runs.get_run(pg, run.id)
    assert reloaded.status == "completed"
    assert reloaded.finished_at is not None
    assert reloaded.summary == {
        "steps": 2,
        "verdicts": {"retried": 1, "passed": 1},
        "retries": 1,
        "escalations": 0,
        "tokens_in": 4,
        "tokens_out": 6,
        "cost_usd": 0.3,
        "latency_ms": 0,
    }


def test_latest_run_picks_the_newest_run_of_a_request(pg: Session) -> None:
    request_id = uuid.uuid4()
    old = runs.start_run(pg, request_id)
    new = runs.start_run(pg, request_id)
    runs.start_run(pg, uuid.uuid4())
    old.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    new.created_at = datetime(2026, 1, 2, tzinfo=UTC)
    pg.commit()

    latest = runs.latest_run(pg, request_id)
    assert latest is not None and latest.id == new.id


@pytest.mark.parametrize(
    ("sql", "constraint"),
    [
        (
            "INSERT INTO workflow_runs (id, request_id, thread_id, status) "
            "VALUES (gen_random_uuid(), gen_random_uuid(), 't', 'bogus')",
            "ck_workflow_runs_run_status",
        ),
    ],
)
def test_run_status_check_constraint(pg: Session, sql: str, constraint: str) -> None:
    with pytest.raises(IntegrityError) as err:
        pg.execute(sa.text(sql))
    assert constraint in str(err.value)


@pytest.mark.parametrize(
    ("verdict", "attempt", "constraint"),
    [
        ("bogus", 1, "ck_agent_steps_step_verdict"),
        ("passed", 0, "ck_agent_steps_attempt_positive"),
    ],
)
def test_agent_step_check_constraints(
    pg: Session, verdict: str, attempt: int, constraint: str
) -> None:
    run = runs.start_run(pg, uuid.uuid4())
    pg.flush()
    with pytest.raises(IntegrityError) as err:
        pg.execute(
            sa.text(
                "INSERT INTO agent_steps "
                "(id, run_id, request_id, node_name, attempt, verdict) "
                "VALUES (gen_random_uuid(), :run, :req, 'n', :attempt, :verdict)"
            ),
            {
                "run": run.id,
                "req": run.request_id,
                "attempt": attempt,
                "verdict": verdict,
            },
        )
    assert constraint in str(err.value)


def test_step_requires_an_existing_run(pg: Session) -> None:
    with pytest.raises(IntegrityError) as err:
        pg.execute(
            sa.text(
                "INSERT INTO agent_steps "
                "(id, run_id, request_id, node_name, attempt, verdict) "
                "VALUES (gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), "
                "'n', 1, 'passed')"
            )
        )
    assert "fk_agent_steps_run_id_workflow_runs" in str(err.value)


def test_deleting_a_run_in_the_database_removes_its_steps(pg: Session) -> None:
    keep = runs.start_run(pg, uuid.uuid4())
    drop = runs.start_run(pg, uuid.uuid4())
    runs.record_step(pg, keep, "writer", 1, "passed")
    runs.record_step(pg, drop, "writer", 1, "passed")
    runs.record_step(pg, drop, "writer", 2, "passed")
    pg.commit()

    pg.execute(sa.delete(WorkflowRun).where(WorkflowRun.id == drop.id))
    pg.commit()

    remaining = pg.scalars(select(AgentStep)).all()
    assert [s.run_id for s in remaining] == [keep.id]


def test_deleting_a_run_through_the_orm_removes_its_steps(pg: Session) -> None:
    run = runs.start_run(pg, uuid.uuid4())
    runs.record_step(pg, run, "writer", 1, "passed")
    pg.commit()

    pg.delete(run)
    pg.commit()

    assert pg.scalars(select(AgentStep)).all() == []
    assert pg.scalars(select(WorkflowRun)).all() == []


def test_indexes_follow_the_database_spec(
    pg: Session, migrated_engine: sa.Engine
) -> None:
    rows = pg.execute(
        sa.text(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE tablename IN ('workflow_runs', 'agent_steps')"
        )
    ).all()
    defs = {name: definition for name, definition in rows}

    assert "(request_id, created_at DESC)" in defs["ix_workflow_runs_request_created"]
    assert "(run_id, created_at)" in defs["ix_agent_steps_run_created"]
    assert "(request_id)" in defs["ix_agent_steps_request_id"]
    assert "(node_name, verdict)" in defs["ix_agent_steps_node_verdict"]
