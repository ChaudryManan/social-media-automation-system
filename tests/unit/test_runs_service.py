"""Unit tests for workflow runs and agent steps (B2-02), on in-memory SQLite."""

import json
import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import sqlalchemy as sa
from app.core.errors import ConflictError, NotFoundError
from app.models import AgentStep, Base, WorkflowRun
from app.services import runs
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture
def session() -> Generator[Session, None, None]:
    engine = sa.create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


@pytest.fixture
def run(session: Session) -> WorkflowRun:
    created = runs.start_run(session, uuid.uuid4())
    session.commit()
    return created


# --------------------------------------------------------------------------
# start_run / get_run / latest_run
# --------------------------------------------------------------------------


def test_start_run_sets_defaults(session: Session) -> None:
    request_id = uuid.uuid4()
    before = datetime.now(UTC)

    run = runs.start_run(session, request_id)
    session.commit()

    assert isinstance(run.id, uuid.UUID)
    assert run.request_id == request_id
    assert run.thread_id == str(request_id)
    assert run.status == "running"
    assert run.started_at is not None
    assert run.started_at.replace(tzinfo=UTC) >= before.replace(microsecond=0)
    assert run.finished_at is None
    assert run.error is None
    assert run.summary is None
    assert run.created_at is not None


def test_start_run_accepts_a_custom_thread_id(session: Session) -> None:
    run = runs.start_run(session, uuid.uuid4(), thread_id="thread-xyz")
    assert run.thread_id == "thread-xyz"


def test_start_run_does_not_commit(session: Session) -> None:
    runs.start_run(session, uuid.uuid4())
    session.rollback()
    assert session.scalars(select(WorkflowRun)).all() == []


def test_get_run_returns_the_run(session: Session, run: WorkflowRun) -> None:
    assert runs.get_run(session, run.id) is run


def test_get_run_raises_not_found(session: Session) -> None:
    with pytest.raises(NotFoundError) as err:
        runs.get_run(session, uuid.uuid4())
    assert err.value.code == "run_not_found"


def test_latest_run_returns_the_newest_run_of_the_request(session: Session) -> None:
    request_id = uuid.uuid4()
    first = runs.start_run(session, request_id)
    second = runs.start_run(session, request_id)
    other = runs.start_run(session, uuid.uuid4())
    first.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    second.created_at = datetime(2026, 1, 2, tzinfo=UTC)
    other.created_at = datetime(2026, 1, 3, tzinfo=UTC)
    session.commit()

    assert runs.latest_run(session, request_id) is second
    assert runs.latest_run(session, uuid.uuid4()) is None


def test_invalid_run_status_is_rejected_by_the_database(session: Session) -> None:
    session.add(WorkflowRun(request_id=uuid.uuid4(), thread_id="t", status="bogus"))
    with pytest.raises(IntegrityError):
        session.flush()


# --------------------------------------------------------------------------
# record_step: inserting and querying a run with steps (B2-02 "done when")
# --------------------------------------------------------------------------


def test_a_run_with_steps_can_be_inserted_and_queried(session: Session) -> None:
    request_id = uuid.uuid4()
    run = runs.start_run(session, request_id)
    runs.record_step(session, run, "writer", 1, "failed", feedback="too long")
    runs.record_step(session, run, "check_writer", 1, "retried", feedback="shorten")
    runs.record_step(session, run, "writer", 2, "passed", model="m-1")
    session.commit()
    run_id = run.id
    session.expunge_all()  # force real reads from the database

    loaded = runs.get_run(session, run_id)
    steps = runs.list_steps(session, run_id)

    assert [(s.node_name, s.attempt, s.verdict) for s in steps] == [
        ("writer", 1, "failed"),
        ("check_writer", 1, "retried"),
        ("writer", 2, "passed"),
    ]
    assert [s.node_name for s in loaded.steps] == [s.node_name for s in steps]
    assert all(s.run_id == run_id for s in steps)
    assert all(s.request_id == request_id for s in steps)
    assert steps[0].feedback == "too long"
    assert steps[2].model == "m-1"


def test_steps_of_different_runs_are_kept_apart(session: Session) -> None:
    a = runs.start_run(session, uuid.uuid4())
    b = runs.start_run(session, uuid.uuid4())
    runs.record_step(session, a, "planner", 1, "passed")
    runs.record_step(session, b, "planner", 1, "passed")
    runs.record_step(session, b, "writer", 1, "passed")
    session.commit()

    assert len(runs.list_steps(session, a.id)) == 1
    assert len(runs.list_steps(session, b.id)) == 2


def test_record_step_stores_all_fields(session: Session, run: WorkflowRun) -> None:
    step = runs.record_step(
        session,
        run,
        "  writer  ",
        2,
        "passed",
        feedback="ok",
        input_summary={"topic": "habits"},
        output_summary={"chars": 120},
        tokens_in=10,
        tokens_out=20,
        cost_usd=0.0012345678,
        latency_ms=350,
        model="model-a, model-b",
    )
    # rounded already on the object the caller gets, not only after a reload
    assert step.cost_usd == Decimal("0.001235")
    session.commit()
    session.refresh(step)

    assert step.node_name == "writer"
    assert step.attempt == 2
    assert step.input_summary == {"topic": "habits"}
    assert step.output_summary == {"chars": 120}
    assert (step.tokens_in, step.tokens_out, step.latency_ms) == (10, 20, 350)
    assert step.cost_usd == Decimal("0.001235")  # rounded to 6 places
    assert step.model == "model-a, model-b"
    assert step.created_at is not None


def test_record_step_defaults(session: Session, run: WorkflowRun) -> None:
    step = runs.record_step(session, run, "validate_input", 1, "passed")
    session.commit()
    session.refresh(step)
    assert (step.tokens_in, step.tokens_out, step.latency_ms) == (0, 0, 0)
    assert step.cost_usd is None
    assert step.feedback is None and step.model is None
    assert step.input_summary is None and step.output_summary is None


def test_record_step_copies_request_id_from_the_run(
    session: Session, run: WorkflowRun
) -> None:
    step = runs.record_step(session, run, "planner", 1, "passed")
    assert step.request_id == run.request_id


def test_record_step_accepts_decimal_cost(session: Session, run: WorkflowRun) -> None:
    step = runs.record_step(session, run, "n", 1, "passed", cost_usd=Decimal("0.5"))
    assert step.cost_usd == Decimal("0.500000")


def test_record_step_does_not_commit(session: Session, run: WorkflowRun) -> None:
    runs.record_step(session, run, "planner", 1, "passed")
    session.rollback()
    assert session.scalars(select(AgentStep)).all() == []


def test_record_step_truncates_long_feedback(
    session: Session, run: WorkflowRun
) -> None:
    step = runs.record_step(
        session, run, "n", 1, "failed", feedback="x" * (runs.MAX_FEEDBACK_CHARS + 50)
    )
    assert step.feedback is not None
    assert len(step.feedback) == runs.MAX_FEEDBACK_CHARS


@pytest.mark.parametrize(
    "kwargs",
    [
        {"node_name": ""},
        {"node_name": "   "},
        {"attempt": 0},
        {"attempt": -1},
        {"attempt": True},
        {"attempt": 1.5},
        {"verdict": "bogus"},
        {"tokens_in": -1},
        {"tokens_out": -5},
        {"latency_ms": -1},
        {"tokens_in": True},
        {"cost_usd": -0.01},
        {"cost_usd": float("nan")},
        {"cost_usd": float("inf")},
    ],
)
def test_record_step_rejects_invalid_values_and_writes_nothing(
    session: Session, run: WorkflowRun, kwargs: dict
) -> None:
    args: dict = {"node_name": "writer", "attempt": 1, "verdict": "passed", **kwargs}
    with pytest.raises(ValueError):
        runs.record_step(
            session,
            run,
            args.pop("node_name"),
            args.pop("attempt"),
            args.pop("verdict"),
            **args,
        )
    assert session.scalars(select(AgentStep)).all() == []


def test_steps_can_be_recorded_while_paused(session: Session, run: WorkflowRun) -> None:
    runs.set_run_status(session, run, "paused")
    runs.record_step(session, run, "human_approval", 1, "escalated")
    assert len(runs.list_steps(session, run.id)) == 1


@pytest.mark.parametrize("final", ["completed", "failed", "cancelled"])
def test_steps_cannot_be_added_to_a_finished_run(
    session: Session, run: WorkflowRun, final: str
) -> None:
    runs.set_run_status(session, run, final)
    with pytest.raises(ConflictError) as err:
        runs.record_step(session, run, "writer", 1, "passed")
    assert err.value.code == "run_finished"
    assert runs.list_steps(session, run.id) == []


@pytest.mark.parametrize(
    ("column", "value"),
    [("verdict", "bogus"), ("attempt", 0)],
)
def test_database_rejects_bad_step_rows(
    session: Session, run: WorkflowRun, column: str, value: object
) -> None:
    values: dict = {"verdict": "passed", "attempt": 1, column: value}
    session.add(
        AgentStep(run_id=run.id, request_id=run.request_id, node_name="n", **values)
    )
    with pytest.raises(IntegrityError):
        session.flush()


# --------------------------------------------------------------------------
# set_run_status
# --------------------------------------------------------------------------


def test_pause_and_resume_do_not_finish_the_run(
    session: Session, run: WorkflowRun
) -> None:
    runs.set_run_status(session, run, "paused")
    assert run.status == "paused" and run.finished_at is None
    runs.set_run_status(session, run, "running")
    assert run.status == "running" and run.finished_at is None
    assert run.summary is None


def test_completing_sets_finished_at_and_summary(
    session: Session, run: WorkflowRun
) -> None:
    runs.record_step(
        session, run, "writer", 1, "passed", tokens_in=5, tokens_out=7, cost_usd=0.25
    )
    runs.set_run_status(session, run, "completed")
    session.commit()
    session.refresh(run)

    assert run.status == "completed"
    assert run.finished_at is not None
    assert run.error is None
    assert run.summary is not None
    assert run.summary["steps"] == 1
    assert run.summary["tokens_in"] == 5
    assert run.summary["cost_usd"] == 0.25


def test_failing_stores_the_error_and_a_summary(
    session: Session, run: WorkflowRun
) -> None:
    runs.record_step(session, run, "writer", 3, "escalated")
    runs.set_run_status(session, run, "failed", error="provider down")
    assert run.status == "failed"
    assert run.error == "provider down"
    assert run.finished_at is not None
    assert run.summary is not None and run.summary["escalations"] == 1


def test_error_is_truncated(session: Session, run: WorkflowRun) -> None:
    runs.set_run_status(session, run, "failed", error="e" * 5000)
    assert run.error is not None and len(run.error) == runs.MAX_ERROR_CHARS


def test_same_status_is_a_noop(session: Session, run: WorkflowRun) -> None:
    runs.set_run_status(session, run, "completed")
    finished = run.finished_at
    summary = run.summary
    runs.set_run_status(session, run, "completed", error="ignored")
    assert run.finished_at == finished
    assert run.summary == summary
    assert run.error is None
    runs.set_run_status(session, runs.start_run(session, uuid.uuid4()), "running")


@pytest.mark.parametrize("final", ["completed", "failed", "cancelled"])
@pytest.mark.parametrize("target", ["running", "paused", "completed", "failed"])
def test_finished_runs_cannot_change_status(
    session: Session, run: WorkflowRun, final: str, target: str
) -> None:
    runs.set_run_status(session, run, final)
    if target == final:
        runs.set_run_status(session, run, target)  # no-op, no error
        return
    with pytest.raises(ConflictError) as err:
        runs.set_run_status(session, run, target)
    assert err.value.code == "run_finished"
    assert run.status == final


def test_unknown_status_is_rejected(session: Session, run: WorkflowRun) -> None:
    with pytest.raises(ValueError):
        runs.set_run_status(session, run, "bogus")
    assert run.status == "running"


# --------------------------------------------------------------------------
# summarize_run
# --------------------------------------------------------------------------


def test_summary_of_an_empty_run(session: Session, run: WorkflowRun) -> None:
    assert runs.summarize_run(session, run) == {
        "steps": 0,
        "verdicts": {},
        "retries": 0,
        "escalations": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_usd": None,
        "latency_ms": 0,
    }


def test_summary_counts_verdicts_and_sums_usage(
    session: Session, run: WorkflowRun
) -> None:
    other = runs.start_run(session, uuid.uuid4())
    runs.record_step(
        session, run, "writer", 1, "failed", tokens_in=10, tokens_out=5, latency_ms=100
    )
    runs.record_step(session, run, "check", 1, "retried", tokens_in=1, cost_usd=0.1)
    runs.record_step(
        session, run, "writer", 2, "passed", tokens_out=9, latency_ms=50, cost_usd=0.2
    )
    runs.record_step(session, run, "writer", 3, "escalated")
    runs.record_step(session, other, "writer", 1, "passed", tokens_in=999)

    summary = runs.summarize_run(session, run)

    assert summary["steps"] == 4
    assert summary["verdicts"] == {
        "failed": 1,
        "retried": 1,
        "passed": 1,
        "escalated": 1,
    }
    assert summary["retries"] == 1
    assert summary["escalations"] == 1
    assert summary["tokens_in"] == 11
    assert summary["tokens_out"] == 14
    assert summary["latency_ms"] == 150
    assert summary["cost_usd"] == pytest.approx(0.3)


def test_summary_cost_is_none_when_no_step_has_a_cost(
    session: Session, run: WorkflowRun
) -> None:
    runs.record_step(session, run, "writer", 1, "passed", tokens_in=3)
    assert runs.summarize_run(session, run)["cost_usd"] is None


# --------------------------------------------------------------------------
# trim_summary: no secrets, no full prompts in the database
# --------------------------------------------------------------------------


def test_trim_summary_passes_none_and_small_values_through() -> None:
    assert runs.trim_summary(None) is None
    small = {"topic": "habits", "n": 3, "ok": True, "x": None, "tags": ["a", "b"]}
    assert runs.trim_summary(small) == small


def test_trim_summary_redacts_secrets_at_any_depth() -> None:
    result = runs.trim_summary(
        {"api_key": "KEY-123", "nested": {"access_token": "TOK", "keep": "yes"}}
    )
    encoded = json.dumps(result)
    assert "KEY-123" not in encoded and "TOK" not in encoded
    assert result["nested"]["keep"] == "yes"


def test_trim_summary_cuts_long_strings() -> None:
    result = runs.trim_summary({"prompt": "p" * 10_000})
    assert len(result["prompt"]) == runs.MAX_SUMMARY_STRING + 1
    assert result["prompt"].endswith("…")


def test_trim_summary_limits_list_and_dict_sizes() -> None:
    result = runs.trim_summary(
        {"items": list(range(100)), **{f"k{i}": i for i in range(40)}}
    )
    assert len(result) == runs.MAX_SUMMARY_ITEMS + 1  # + "_more"
    assert result["_more"] == 41 - runs.MAX_SUMMARY_ITEMS
    assert len(result["items"]) == runs.MAX_SUMMARY_ITEMS + 1
    assert result["items"][-1] == f"…(+{100 - runs.MAX_SUMMARY_ITEMS} more)"


def test_trim_summary_limits_depth() -> None:
    deep: dict = {}
    cursor = deep
    for _ in range(10):
        cursor["a"] = {}
        cursor = cursor["a"]

    result = runs.trim_summary(deep)

    levels = 0
    while isinstance(result, dict):
        result = result["a"]
        levels += 1
    assert levels == runs.MAX_SUMMARY_DEPTH
    assert result == "…"


def test_trim_summary_replaces_huge_payloads_with_a_preview() -> None:
    # 20 keys x 20 items x 400 chars is far above the size cap even after trimming
    huge = {f"k{i}": ["z" * 400 for _ in range(20)] for i in range(20)}
    result = runs.trim_summary(huge)
    assert result["_truncated"] is True
    assert len(result["preview"]) == 2000
    assert len(json.dumps(result)) < runs.MAX_SUMMARY_JSON_CHARS


def test_trim_summary_makes_values_json_safe() -> None:
    uid = uuid.uuid4()
    result = runs.trim_summary(
        {
            "id": uid,
            "when": datetime(2026, 1, 2, tzinfo=UTC),
            "cost": Decimal("1.5"),
            "tags": ("a", "b"),
            "obj": object(),
        }
    )
    json.dumps(result)  # must not raise
    assert result["id"] == str(uid)
    assert result["when"].startswith("2026-01-02")
    assert result["cost"] == "1.5"
    assert result["tags"] == ["a", "b"]


def test_record_step_stores_trimmed_and_redacted_summaries(
    session: Session, run: WorkflowRun
) -> None:
    step = runs.record_step(
        session,
        run,
        "writer",
        1,
        "passed",
        input_summary={"password": "hunter2", "prompt": "q" * 5000},
        output_summary={"text": "t" * 5000},
    )
    session.commit()
    session.refresh(step)
    assert "hunter2" not in json.dumps(step.input_summary)
    assert len(step.input_summary["prompt"]) <= runs.MAX_SUMMARY_STRING + 1
    assert len(step.output_summary["text"]) <= runs.MAX_SUMMARY_STRING + 1
