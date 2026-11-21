"""The main graph on the real LangGraph Postgres saver (B2-08).

The checkpoint tables are created by the library's ``setup()`` (not Alembic).
To keep the migrated test schema untouched they live in their own schema
(``ckpt_test``), selected through the connection's ``search_path``.
"""

from collections.abc import Generator
from typing import Any

import pytest
import sqlalchemy as sa
from app.agents.graph import compile_graph, postgres_checkpointer, thread_config
from app.agents.llm import FakeScript
from app.agents.nodes.researcher import make_researcher_node
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
        saver, researcher=make_researcher_node(search_provider=lambda: search)
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
