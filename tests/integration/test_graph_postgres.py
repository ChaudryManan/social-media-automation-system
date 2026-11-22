"""The main graph on the real LangGraph Postgres saver (B2-08).

The checkpoint tables are created by the library's ``setup()`` (not Alembic).
To keep the migrated test schema untouched they live in their own schema
(``ckpt_test``), selected through the connection's ``search_path``.
"""

from collections.abc import Generator
from typing import Any

import pytest
import sqlalchemy as sa
from app.agents.checked import StepRecord
from app.agents.fake_flow import post_flow_replies
from app.agents.graph import compile_graph, postgres_checkpointer, thread_config
from app.agents.llm import FakeCall, FakeScript
from app.agents.nodes.researcher import make_researcher_node
from app.agents.post import make_post_subgraph
from app.providers.search import FakeSearch
from langgraph.types import Command

pytestmark = pytest.mark.integration

SCHEMA = "ckpt_test"
JUDGE = {"ok": True, "score": 0.9, "issues": [], "feedback": ""}
TABLES = {
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoint_migrations",
}


@pytest.fixture
def ckpt_url(migrated_engine: sa.Engine) -> Generator[str, None, None]:
    """The test database URL whose ``search_path`` is an empty private schema."""
    with migrated_engine.begin() as conn:
        conn.execute(sa.text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        conn.execute(sa.text(f"CREATE SCHEMA {SCHEMA}"))
    base = migrated_engine.url.render_as_string(hide_password=False)
    try:
        yield f"{base}?options=-csearch_path%3D{SCHEMA}"
    finally:
        with migrated_engine.begin() as conn:
            conn.execute(sa.text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))


def tables(engine: sa.Engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = :s"
            ),
            {"s": SCHEMA},
        )
        return {r[0] for r in rows}


def plan() -> dict[str, Any]:
    return {
        "angle": "Green tea is a simple daily habit",
        "audience": "busy young professionals",
        "goal": "education",
        "key_messages": ["Easy to start", "Small habit, steady benefit"],
        "tone": "warm and simple",
        "cta": "Try one cup tomorrow",
        "format_notes": "short image post",
        "must_avoid": [],
        "risks": [],
        "language": "en",
    }


def notes() -> dict[str, Any]:
    return {
        "facts": [
            {
                "claim": "Tea is widely drunk",
                "source_url": "https://example.com/1",
                "source_title": "Source 1",
                "confidence": "high",
            }
        ],
        "trends": [],
        "angles": [],
        "cautions": [],
        "grounded": True,
    }


def strategy(hashtags: int) -> dict[str, Any]:
    return {
        p: {
            "platform": p,
            "tone": "warm",
            "structure": "hook, short lines, CTA",
            "length_target": 600,
            "cta": "Try it",
            "hashtag_count": hashtags,
            "media_notes": "bright shots",
            "posting_notes": "morning",
            "video": None,
        }
        for p in ("facebook", "instagram")
    }


def request() -> dict[str, Any]:
    return {
        "request_id": "req-1",
        "workspace_id": "w1",
        "user_id": "u1",
        "topic": "Benefits of green tea",
        "content_type": "post",
        "target_accounts": [
            {"account_id": "a1", "platform": "facebook"},
            {"account_id": "a2", "platform": "instagram"},
        ],
        "publish_at": "2026-12-01T10:00:00Z",
        "brand_voice": "warm and simple",
        "language": "en",
    }


def graph(saver: Any) -> Any:
    """A fresh compiled graph with fresh node objects (as a new worker would build)."""
    search = FakeSearch()
    return compile_graph(
        saver,
        researcher=make_researcher_node(search_provider=lambda: search),
        with_review=False,  # these tests cover the steps before the reviewer (B2-11)
    )


def test_setup_creates_the_checkpoint_tables_and_is_idempotent(
    ckpt_url: str, migrated_engine: sa.Engine
) -> None:
    with postgres_checkpointer(ckpt_url):
        pass
    with postgres_checkpointer(ckpt_url):  # a second setup() must not fail
        pass

    assert TABLES <= tables(migrated_engine)


def test_setup_can_be_skipped(ckpt_url: str, migrated_engine: sa.Engine) -> None:
    with postgres_checkpointer(ckpt_url, setup=False):
        pass

    assert tables(migrated_engine) == set()


def test_a_post_run_is_checkpointed_under_the_request_id(
    fake_llm: FakeScript, ckpt_url: str, migrated_engine: sa.Engine
) -> None:
    fake_llm.queue(
        plan(), JUDGE, {"queries": ["q"]}, notes(), JUDGE, strategy(5), JUDGE
    )
    config = thread_config("req-1")

    with postgres_checkpointer(ckpt_url) as saver:
        graph(saver).invoke(request(), config)

    with postgres_checkpointer(ckpt_url) as saver:  # a later connection
        values = graph(saver).get_state(config).values
        other = graph(saver).get_state(thread_config("req-2")).values

    assert set(values["strategy"]) == {"facebook", "instagram"}
    assert values["drafts"] == {}  # the post placeholder ran
    assert other == {}
    with migrated_engine.connect() as conn:
        threads = conn.execute(
            sa.text(f"SELECT DISTINCT thread_id FROM {SCHEMA}.checkpoints")
        ).scalars()
        assert set(threads) == {"req-1"}


def test_an_escalated_run_resumes_after_a_worker_restart(
    fake_llm: FakeScript, ckpt_url: str
) -> None:
    fake_llm.queue(
        plan(), JUDGE, {"queries": ["q"]}, notes(), JUDGE,
        strategy(99), strategy(99), strategy(99),  # three failed attempts
    )  # fmt: skip
    config = thread_config("req-1")

    with postgres_checkpointer(ckpt_url) as saver:
        paused = graph(saver).invoke(request(), config)
    (interrupt,) = paused["__interrupt__"]
    assert interrupt.value["step"] == "strategist"
    assert interrupt.value["reason"] == "retries_exhausted"

    # "Restart": a new connection, new saver, new graph and node objects.
    fake_llm.queue(strategy(5), JUDGE)
    with postgres_checkpointer(ckpt_url) as saver:
        compiled = graph(saver)
        assert compiled.get_state(config).next == ("escalate_strategist",)
        compiled.invoke(Command(resume={"action": "retry"}), config)
        final = compiled.get_state(config)

    assert final.next == ()
    assert final.values["strategy"]["facebook"]["hashtag_count"] == 5
    assert final.values["drafts"] == {}
    assert final.values["plan"]["goal"] == "education"  # earlier steps kept


def test_the_default_database_url_comes_from_settings(
    ckpt_url: str, monkeypatch: pytest.MonkeyPatch, migrated_engine: sa.Engine
) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("DATABASE_URL", ckpt_url)
    get_settings.cache_clear()
    try:
        with postgres_checkpointer():
            pass
    finally:
        get_settings.cache_clear()

    assert TABLES <= tables(migrated_engine)


# --------------------------------------------------------------------------
# The whole flow (real post subgraph and reviewer) on the Postgres saver (B2-13)
# --------------------------------------------------------------------------

FULL_FLOW = pytest.mark.usefixtures("media_root", "fake_image")
PLATFORMS = ["facebook", "instagram"]


def full_graph(saver: Any) -> Any:
    """Fresh node objects with the real post subgraph and the reviewer."""
    search = FakeSearch()
    return compile_graph(
        saver,
        researcher=make_researcher_node(search_provider=lambda: search),
        post_subgraph=make_post_subgraph(),
    )


def script_flow(
    fake_llm: FakeScript, *, bad_writer_calls: int = 0, review_error: bool = False
) -> dict[str, int]:
    """The dev runner's valid replies; the first ``bad_writer_calls`` writer calls
    are too short and, with ``review_error``, the first review flags the hashtags."""
    base = post_flow_replies("Benefits of green tea", "en", PLATFORMS)
    seen = {"writer": 0, "reviewer": 0}

    def answer(call: FakeCall) -> dict[str, Any]:
        system = call.messages[0].content
        reply = base(call)
        if "copywriter" in system:
            seen["writer"] += 1
            if seen["writer"] <= bad_writer_calls:
                return {**reply, "caption": "Too short."}
        if "final reviewer" in system:
            seen["reviewer"] += 1
            if review_error and seen["reviewer"] == 1:
                issue = {
                    "severity": "error",
                    "platform": "instagram",
                    "responsible_node": "hashtag",
                    "message": "hashtag problem",
                    "fix_hint": "fix the hashtag",
                }
                return {**reply, "ok": False, "issues": [issue]}
        return reply

    fake_llm.set_default(answer)
    return seen


@FULL_FLOW
def test_a_full_post_run_with_review_is_readable_from_a_later_connection(
    fake_llm: FakeScript, ckpt_url: str
) -> None:
    script_flow(fake_llm)
    config = thread_config("req-1")

    with postgres_checkpointer(ckpt_url) as saver:
        result = full_graph(saver).invoke(request(), config)
    assert "__interrupt__" not in result

    with postgres_checkpointer(ckpt_url) as saver:  # a later connection
        final = full_graph(saver).get_state(config)

    assert final.next == ()
    assert set(final.values["drafts"]) == {"facebook", "instagram"}
    assert final.values["review"]["ok"] is True
    for draft in final.values["drafts"].values():
        assert draft["caption"] and draft["hashtags"] and len(draft["media"]) == 1


@FULL_FLOW
def test_an_escalation_in_the_post_subgraph_resumes_after_a_worker_restart(
    fake_llm: FakeScript, ckpt_url: str
) -> None:
    script_flow(fake_llm, bad_writer_calls=6)  # three failed attempts, two platforms
    config = thread_config("req-1")

    with postgres_checkpointer(ckpt_url) as saver:
        paused = full_graph(saver).invoke(request(), config)
    (interrupt,) = paused["__interrupt__"]
    assert interrupt.value["step"] == "writer"
    assert interrupt.value["reason"] == "retries_exhausted"

    # "Restart": new connection, new saver, new graph and node objects.
    with postgres_checkpointer(ckpt_url) as saver:
        compiled = full_graph(saver)
        parent = compiled.get_state(config, subgraphs=True)
        assert parent.next == ("post_subgraph",)  # paused inside the subgraph
        (task,) = parent.tasks
        assert task.state.next == ("escalate_writer",)
        done = compiled.invoke(Command(resume={"action": "retry"}), config)
        final = compiled.get_state(config)

    assert "__interrupt__" not in done
    assert final.next == ()
    assert final.values["plan"]["goal"] == "awareness"  # earlier steps were kept
    assert set(final.values["drafts"]) == {"facebook", "instagram"}
    assert final.values["review"]["ok"] is True
    assert final.values["retries"]["writer"] == 0  # reset by the human's retry


@FULL_FLOW
def test_an_abort_after_a_worker_restart_ends_the_run_as_failed(
    fake_llm: FakeScript, ckpt_url: str
) -> None:
    script_flow(fake_llm, bad_writer_calls=6)
    config = thread_config("req-1")
    with postgres_checkpointer(ckpt_url) as saver:
        full_graph(saver).invoke(request(), config)

    with postgres_checkpointer(ckpt_url) as saver:
        compiled = full_graph(saver)
        compiled.invoke(Command(resume={"action": "abort"}), config)
        final = compiled.get_state(config)

    assert final.next == ()
    assert final.values["status"] == "failed"
    assert any("writer" in error for error in final.values["errors"])
    assert "review" not in final.values or final.values["review"] is None


@FULL_FLOW
def test_the_review_fix_loop_is_checkpointed_and_step_rows_are_recorded(
    fake_llm: FakeScript, ckpt_url: str
) -> None:
    script_flow(fake_llm, review_error=True)
    records: list[StepRecord] = []
    config = thread_config("req-1", step_recorder=records.append)

    with postgres_checkpointer(ckpt_url) as saver:
        result = full_graph(saver).invoke(request(), config)
    with postgres_checkpointer(ckpt_url) as saver:
        final = full_graph(saver).get_state(thread_config("req-1"))

    assert "__interrupt__" not in result
    assert [(r.node_name, r.attempt) for r in records if r.node_name == "hashtag"] == [
        ("hashtag", 1),
        ("hashtag", 2),
    ]
    assert final.values["review_cycles"] == 1
    assert final.values["rerun_from"] is None
    assert final.values["review"]["ok"] is True
