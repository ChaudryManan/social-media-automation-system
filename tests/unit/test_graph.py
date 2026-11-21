"""Tests for ``app.agents.graph`` (B2-08): router, wiring, placeholders, helpers.

Everything runs offline: the fake LLM, ``FakeSearch`` and ``InMemorySaver``.
Replies are queued in node order: planner (plan, judge), researcher (queries,
summary, judge), strategist (strategy, judge).
"""

import uuid
from typing import Any

import pytest
from app.agents.graph import (
    NODE_CONTENT_ROUTER,
    NODE_POST_SUBGRAPH,
    NODE_VALIDATE_INPUT,
    NODE_VIDEO_SUBGRAPH,
    build_graph,
    compile_graph,
    content_router,
    post_placeholder,
    route_content_type,
    thread_config,
    to_psycopg_url,
    video_placeholder,
)
from app.agents.llm import FakeScript
from app.agents.nodes.planner import make_planner_node
from app.agents.nodes.researcher import make_researcher_node
from app.agents.nodes.strategist import make_strategist_node
from app.agents.state import GraphState
from app.providers.search import FakeSearch
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

JUDGE = {"ok": True, "score": 0.9, "issues": [], "feedback": ""}


def plan() -> dict[str, Any]:
    return {
        "angle": "Green tea is a simple daily habit",
        "audience": "busy young professionals",
        "goal": "education",
        "key_messages": ["Easy to start", "Small habit, steady benefit"],
        "tone": "warm and simple",
        "cta": "Try one cup tomorrow",
        "format_notes": "short image post",
        "must_avoid": ["health guarantees"],
        "risks": ["health claims need a source"],
        "language": "en",
    }


def queries() -> dict[str, Any]:
    return {"queries": ["green tea benefits", "tea habits"]}


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
        "trends": ["Tea at work"],
        "angles": ["Focus ritual"],
        "cautions": ["Effects vary"],
        "grounded": True,
    }


def strat(platform: str, video: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "platform": platform,
        "tone": "warm",
        "structure": "hook, short lines, CTA",
        "length_target": 30 if video else 600,
        "cta": "Try it",
        "hashtag_count": 5,
        "media_notes": "bright shots",
        "posting_notes": "morning",
        "video": video,
    }


def video_part() -> dict[str, Any]:
    return {
        "format": "short",
        "aspect_ratio": "9:16",
        "target_duration_s": 30,
        "scene_count": 4,
        "style": "warm kitchen shots",
    }


def queue_happy_path(fake_llm: FakeScript, *, video: bool = False) -> None:
    platforms = ("instagram", "tiktok") if video else ("facebook", "instagram")
    strategy = {p: strat(p, video_part() if video else None) for p in platforms}
    fake_llm.queue(plan(), JUDGE, queries(), notes(), JUDGE, strategy, JUDGE)


def state(content_type: str = "post", **changes: Any) -> GraphState:
    platforms = (
        ("instagram", "tiktok")
        if content_type == "video"
        else (
            "facebook",
            "instagram",
        )
    )
    base: dict[str, Any] = {
        "request_id": "r1",
        "workspace_id": "w1",
        "user_id": "u1",
        "topic": "Benefits of green tea",
        "content_type": content_type,
        "target_accounts": [
            {"account_id": f"a{i}", "platform": p} for i, p in enumerate(platforms)
        ],
        "publish_at": "2026-12-01T10:00:00Z",
        "brand_voice": "warm and simple",
        "language": "en",
    }
    base.update(changes)
    return base  # type: ignore[return-value]


class Run:
    """A compiled main graph with offline parts and an in-memory checkpointer."""

    def __init__(self, **parts: Any) -> None:
        self.search = FakeSearch()
        self.planner = parts.pop("planner", None) or make_planner_node()
        self.researcher = parts.pop("researcher", None) or make_researcher_node(
            search_provider=lambda: self.search
        )
        self.strategist = parts.pop("strategist", None) or make_strategist_node()
        self.graph = compile_graph(
            InMemorySaver(),
            planner=self.planner,
            researcher=self.researcher,
            strategist=self.strategist,
            **parts,
        )
        self.config = thread_config(str(uuid.uuid4()))

    def stream(self, initial: GraphState) -> list[tuple[str, dict[str, Any]]]:
        """(node name, update) for every node that ran, in order."""
        events: list[tuple[str, dict[str, Any]]] = []
        for chunk in self.graph.stream(initial, self.config, stream_mode="updates"):
            for name, update in chunk.items():
                if name != "__interrupt__":
                    events.append((name, update or {}))
        return events

    def names(self, initial: GraphState) -> list[str]:
        return [name for name, _ in self.stream(initial)]

    def values(self) -> dict[str, Any]:
        return self.graph.get_state(self.config).values


# --------------------------------------------------------------------------
# Router
# --------------------------------------------------------------------------


@pytest.mark.parametrize("content_type", ["post", "video"])
def test_the_router_returns_the_content_type(content_type: str) -> None:
    assert route_content_type(state(content_type)) == content_type


@pytest.mark.parametrize("value", [None, "", "story", "Post", 3])
def test_an_unknown_content_type_is_a_bug_not_a_silent_default(value: Any) -> None:
    with pytest.raises(ValueError, match="content_type"):
        route_content_type(state(content_type=value))


def test_a_missing_content_type_is_rejected() -> None:
    base = state()
    del base["content_type"]  # type: ignore[misc]

    with pytest.raises(ValueError, match="content_type"):
        route_content_type(base)


def test_the_router_ignores_rerun_from_and_leaves_it_alone() -> None:
    assert route_content_type(state("video", rerun_from="writer")) == "video"
    assert content_router(state(rerun_from="writer")) == {}


# --------------------------------------------------------------------------
# Placeholders and helpers
# --------------------------------------------------------------------------


def test_the_placeholders_write_no_content() -> None:
    assert post_placeholder(state()) == {"drafts": {}}
    assert video_placeholder(state("video")) == {"drafts": {}}


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("postgresql+psycopg://u:p@h:15432/db", "postgresql://u:p@h:15432/db"),
        ("postgresql://u:p@h/db", "postgresql://u:p@h/db"),
        ("postgres+psycopg2://u@h/db?x=1", "postgres://u@h/db?x=1"),
    ],
)
def test_the_sqlalchemy_url_becomes_a_psycopg_url(given: str, expected: str) -> None:
    assert to_psycopg_url(given) == expected


def test_a_url_without_a_scheme_is_rejected() -> None:
    with pytest.raises(ValueError, match="database_url"):
        to_psycopg_url("localhost/db")


def test_the_thread_id_is_the_request_id() -> None:
    assert thread_config("req-9") == {"configurable": {"thread_id": "req-9"}}
    assert thread_config("req-9", run_id="r")["configurable"] == {
        "thread_id": "req-9",
        "run_id": "r",
    }


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


def test_the_default_graph_compiles_with_every_node() -> None:
    graph = compile_graph(None)

    nodes = set(graph.get_graph().nodes)

    assert {
        NODE_VALIDATE_INPUT,
        NODE_CONTENT_ROUTER,
        NODE_POST_SUBGRAPH,
        NODE_VIDEO_SUBGRAPH,
        "planner",
        "researcher",
        "strategist",
        "check_planner",
        "escalate_strategist",
    } <= nodes


def test_build_graph_returns_an_uncompiled_builder() -> None:
    builder = build_graph()

    assert hasattr(builder, "compile")
    assert NODE_CONTENT_ROUTER in builder.nodes


# --------------------------------------------------------------------------
# Running the graph
# --------------------------------------------------------------------------


def test_a_post_request_reaches_the_post_placeholder(fake_llm: FakeScript) -> None:
    queue_happy_path(fake_llm)
    run = Run()

    names = run.names(state("post"))

    assert names == [
        "validate_input",
        "planner",
        "check_planner",
        "researcher",
        "check_researcher",
        "strategist",
        "check_strategist",
        "content_router",
        "post_subgraph",
    ]
    assert "video_subgraph" not in names
    assert set(run.values()["strategy"]) == {"facebook", "instagram"}


def test_a_video_request_reaches_the_video_placeholder(fake_llm: FakeScript) -> None:
    queue_happy_path(fake_llm, video=True)
    run = Run()

    names = run.names(state("video"))

    assert names[-2:] == ["content_router", "video_subgraph"]
    assert "post_subgraph" not in names
    assert run.values()["strategy"]["tiktok"]["video"]["scene_count"] == 4


def test_the_state_carries_every_agent_output_to_the_end(fake_llm: FakeScript) -> None:
    queue_happy_path(fake_llm)
    run = Run()

    run.names(state())
    values = run.values()

    assert values["plan"]["goal"] == "education"
    assert values["research"]["grounded"] is True
    assert values["drafts"] == {}
    assert values["status"] == "generating"
    assert run.search.calls == ["green tea benefits", "tea habits"]
    assert run.graph.get_state(run.config).next == ()  # finished, nothing pending


def test_every_agent_step_runs_in_order_with_the_right_llm_calls(
    fake_llm: FakeScript,
) -> None:
    queue_happy_path(fake_llm)

    Run().names(state())

    assert [c.kind for c in fake_llm.calls] == [
        "text", "fast",  # planner, judge
        "text", "text", "fast",  # researcher queries, summary, judge
        "text", "fast",  # strategist, judge
    ]  # fmt: skip


def test_an_invalid_request_ends_at_validation_without_any_llm_call(
    fake_llm: FakeScript,
) -> None:
    run = Run()

    names = run.names(state(topic="   "))

    assert names == ["validate_input"]
    assert fake_llm.calls == []
    assert run.search.calls == []
    assert run.values()["status"] == "failed"
    assert run.values()["errors"]


def test_a_platform_that_cannot_take_the_content_type_ends_at_validation(
    fake_llm: FakeScript,
) -> None:
    run = Run()
    post_to_youtube = state(
        "post", target_accounts=[{"account_id": "a1", "platform": "youtube"}]
    )

    assert run.names(post_to_youtube) == ["validate_input"]
    assert run.values()["status"] == "failed"
    assert fake_llm.calls == []


def test_rerun_from_is_left_untouched_by_the_shared_part(fake_llm: FakeScript) -> None:
    queue_happy_path(fake_llm)
    run = Run()

    run.names(state(rerun_from="writer"))

    assert run.values()["rerun_from"] == "writer"


def test_real_subgraphs_can_replace_the_placeholders(fake_llm: FakeScript) -> None:
    queue_happy_path(fake_llm)
    seen: dict[str, Any] = {}

    def post_subgraph(s: GraphState) -> dict[str, Any]:
        seen["strategy"] = s["strategy"]
        seen["plan"] = s["plan"]
        return {"drafts": {"facebook": {"caption": "Hello"}}}

    run = Run(post_subgraph=post_subgraph)

    names = run.names(state())

    assert names[-1] == "post_subgraph"
    assert set(seen["strategy"]) == {"facebook", "instagram"}
    assert seen["plan"]["angle"].startswith("Green tea")
    assert run.values()["drafts"] == {"facebook": {"caption": "Hello"}}


def test_a_custom_video_subgraph_is_used_for_video(fake_llm: FakeScript) -> None:
    queue_happy_path(fake_llm, video=True)
    run = Run(video_subgraph=lambda s: {"drafts": {"tiktok": {"title": "T"}}})

    run.names(state("video"))

    assert run.values()["drafts"] == {"tiktok": {"title": "T"}}


def test_a_custom_validate_node_replaces_the_default(fake_llm: FakeScript) -> None:
    def reject(s: GraphState) -> dict[str, Any]:
        return {"status": "failed", "errors": ["no"]}

    run = Run(validate_input=reject)

    assert run.names(state()) == ["validate_input"]
    assert fake_llm.calls == []


def test_a_failing_step_escalates_and_resume_continues_to_the_router(
    fake_llm: FakeScript,
) -> None:
    bad = {p: strat(p) for p in ("facebook", "instagram")}
    for item in bad.values():
        item["hashtag_count"] = 99  # over the platform maximum
    good = {p: strat(p) for p in ("facebook", "instagram")}
    fake_llm.queue(plan(), JUDGE, queries(), notes(), JUDGE, bad, bad, bad, good, JUDGE)
    run = Run()

    first = run.names(state())

    assert first[-1] == "check_strategist"  # the interrupt emits no update
    pending = run.graph.get_state(run.config)
    assert pending.next == ("escalate_strategist",)
    assert pending.tasks[0].interrupts[0].value["step"] == "strategist"

    resumed = [
        name
        for chunk in run.graph.stream(
            Command(resume={"action": "retry"}), run.config, stream_mode="updates"
        )
        for name in chunk
    ]

    assert resumed[-2:] == ["content_router", "post_subgraph"]
    assert run.values()["strategy"]["facebook"]["hashtag_count"] == 5


def test_aborting_an_escalated_step_ends_the_run_as_failed(
    fake_llm: FakeScript,
) -> None:
    bad = {p: strat(p) for p in ("facebook", "instagram")}
    for item in bad.values():
        item["hashtag_count"] = 99
    fake_llm.queue(plan(), JUDGE, queries(), notes(), JUDGE, bad, bad, bad)
    run = Run()
    run.names(state())

    run.graph.invoke(Command(resume={"action": "abort"}), run.config)

    values = run.values()
    assert values["status"] == "failed"
    assert "drafts" not in values  # never reached the content subgraph
    assert run.graph.get_state(run.config).next == ()


def test_two_requests_do_not_share_state(fake_llm: FakeScript) -> None:
    queue_happy_path(fake_llm)
    run = Run()
    run.names(state())

    other = thread_config("another-request")

    assert run.graph.get_state(other).values == {}
    assert run.graph.get_state(run.config).values["plan"]
