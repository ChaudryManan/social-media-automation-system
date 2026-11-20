"""The checked node writing ``agent_steps`` on real PostgreSQL (B2-04).

Uses the production path: only ``run_id`` is in the run config, so the node
builds the database recorder itself and opens its own sessions.
"""

import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from app.agents import checked
from app.agents.checked import (
    AgentOutput,
    CheckResult,
    Issue,
    add_checked_node,
    make_checked_node,
)
from app.agents.llm import FakeScript, generate_structured, user_message
from app.agents.state import GraphState
from app.services import runs
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.integration


class Tags(BaseModel):
    hashtags: list[str]


@pytest.fixture
def sessions(
    pg: Session, migrated_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch
) -> sessionmaker[Session]:
    """Make the node's own ``SessionLocal`` point at the migrated test database."""
    factory = sessionmaker(bind=migrated_engine, autoflush=False)
    import app.core.db as db_module

    monkeypatch.setattr(db_module, "SessionLocal", factory)
    return factory


def build_graph(node: Any) -> Any:
    builder = StateGraph(GraphState)
    entry = add_checked_node(builder, node, on_pass=END)
    builder.add_edge(START, entry)
    return builder.compile(checkpointer=InMemorySaver())


def test_attempts_are_stored_with_usage_and_summaries(
    pg: Session, sessions: sessionmaker[Session], fake_llm: FakeScript
) -> None:
    run = runs.start_run(pg, uuid.uuid4())
    pg.commit()
    run_id = run.id
    fake_llm.queue({"hashtags": ["#a"]}, {"hashtags": ["#a", "#b"]})

    def agent(state: GraphState) -> AgentOutput:
        out = generate_structured(Tags, [user_message("write tags")])
        return AgentOutput(
            update={"drafts": {"instagram": {"hashtags": out.hashtags}}},
            prompt_version="hashtag.v1",
        )

    def check(state: GraphState) -> CheckResult:
        if len(state["drafts"]["instagram"]["hashtags"]) < 2:
            return CheckResult.from_issues(
                [Issue(code="few", message="Use two hashtags.", severity="error")]
            )
        return CheckResult(ok=True)

    graph = build_graph(make_checked_node("hashtag", agent, check))

    graph.invoke(
        {"topic": "t"},
        {"configurable": {"thread_id": "t1", "run_id": str(run_id)}},
    )

    with sessions() as db:
        rows = runs.list_steps(db, run_id)
        assert [(r.node_name, r.attempt, r.verdict) for r in rows] == [
            ("hashtag", 1, "retried"),
            ("hashtag", 2, "passed"),
        ]
        first, second = rows
        assert first.feedback == "Use two hashtags."
        assert first.model == "fake-text"
        assert first.tokens_in == 2  # the fake provider counts the words of the prompt
        assert first.tokens_out > 0
        assert first.cost_usd is not None and float(first.cost_usd) == 0.0
        assert first.output_summary["prompt_version"] == "hashtag.v1"
        assert first.output_summary["issues"][0]["code"] == "few"
        assert second.input_summary["prior_feedback"] == "Use two hashtags."
        assert second.feedback is None
        assert all(r.request_id == run.request_id for r in rows)


def test_escalation_and_abort_are_stored_and_summarised(
    pg: Session, sessions: sessionmaker[Session]
) -> None:
    run = runs.start_run(pg, uuid.uuid4())
    pg.commit()
    run_id = run.id
    node = make_checked_node(
        "writer",
        lambda s: {"drafts": {}},
        lambda s: CheckResult.from_issues(
            [Issue(code="x", message="Fix it.", severity="error")]
        ),
        2,
    )
    graph = build_graph(node)
    config = {"configurable": {"thread_id": "t2", "run_id": str(run_id)}}

    paused = graph.invoke({}, config)
    assert paused["__interrupt__"][0].value["type"] == "needs_attention"
    graph.invoke(Command(resume={"action": "abort"}), config)

    with sessions() as db:
        run_row = runs.get_run(db, run_id)
        assert [r.verdict for r in runs.list_steps(db, run_id)] == [
            "retried",
            "escalated",
            "failed",
        ]
        runs.set_run_status(db, run_row, "failed")
        db.commit()
        assert run_row.summary["retries"] == 1
        assert run_row.summary["escalations"] == 1
        assert run_row.summary["verdicts"]["failed"] == 1


def test_a_finished_run_does_not_stop_the_workflow(
    pg: Session, sessions: sessionmaker[Session], caplog: pytest.LogCaptureFixture
) -> None:
    run = runs.start_run(pg, uuid.uuid4())
    runs.set_run_status(pg, run, "cancelled")
    pg.commit()
    node = make_checked_node(
        "writer", lambda s: {"drafts": {}}, lambda s: CheckResult(ok=True)
    )
    graph = build_graph(node)

    result = graph.invoke(
        {}, {"configurable": {"thread_id": "t3", "run_id": str(run.id)}}
    )

    assert result["attempt_info"]["writer"]["outcome"] == "pass"
    assert "Could not record agent step" in caplog.text
    with sessions() as db:
        assert runs.list_steps(db, run.id) == []


def test_database_recorder_is_selected_by_run_id_only(
    pg: Session, sessions: sessionmaker[Session]
) -> None:
    run = runs.start_run(pg, uuid.uuid4())
    pg.commit()

    recorder = checked.resolve_step_recorder({"configurable": {"run_id": str(run.id)}})

    assert recorder is not None
    recorder(
        checked.StepRecord("writer", 1, "passed", None, None, None, 5, 6, 0.5, 7, "m")
    )
    with sessions() as db:
        (row,) = runs.list_steps(db, run.id)
        assert (row.tokens_in, row.tokens_out, row.latency_ms) == (5, 6, 7)
