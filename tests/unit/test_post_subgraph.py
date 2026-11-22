"""Tests for the post subgraph and its place in the main graph (B2-10), offline."""

import uuid
from typing import Any

import pytest
from app.agents.graph import compile_graph, thread_config
from app.agents.llm import FakeCall, FakeScript
from app.agents.nodes.planner import make_planner_node
from app.agents.nodes.researcher import make_researcher_node
from app.agents.nodes.strategist import make_strategist_node
from app.agents.post import (
    make_hashtag_node,
    make_image_node,
    make_post_subgraph,
    make_writer_node,
)
from app.agents.post.subgraph import (
    route_after_image,
    route_after_writer,
    route_post_entry,
)
from app.agents.state import GraphState
from app.providers.image import FakeImage, build_png, read_image_info
from app.providers.search import FakeSearch
from app.storage import get_storage_backend
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

pytestmark = pytest.mark.usefixtures("media_root", "fake_image")

JUDGE = {"ok": True, "score": 0.9, "issues": [], "feedback": ""}
CAPTION = ("Green tea is an easy daily habit. " * 17).strip()
TAGS = {
    "facebook": ["#greentea", "#tea"],
    "instagram": ["#greentea", "#tea", "#morning", "#wellness", "#habits"],
}


def strat(platform: str) -> dict[str, Any]:
    return {
        "platform": platform,
        "tone": "warm",
        "structure": "hook, short lines, CTA",
        "length_target": 600,
        "cta": "Try it",
        "hashtag_count": len(TAGS[platform]),
        "media_notes": "bright shots",
        "posting_notes": "morning",
        "video": None,
    }


def state(**changes: Any) -> GraphState:
    base: dict[str, Any] = {
        "request_id": "r1",
        "workspace_id": "w1",
        "user_id": "u1",
        "topic": "Benefits of green tea",
        "content_type": "post",
        "target_accounts": [
            {"account_id": "a1", "platform": "instagram"},
            {"account_id": "a2", "platform": "facebook"},
            {"account_id": "a3", "platform": "instagram"},
        ],
        "publish_at": "2026-12-01T10:00:00Z",
        "brand_voice": "warm and simple",
        "language": "en",
        "plan": {"angle": "Tea is a daily habit"},
        "research": {"facts": [], "grounded": False, "cautions": ["avoid statistics"]},
        "strategy": {p: strat(p) for p in ("facebook", "instagram")},
    }
    base.update(changes)
    return base  # type: ignore[return-value]


def review_reply(*issues: dict[str, Any], **changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "ok": not any(i["severity"] == "error" for i in issues),
        "scores": dict.fromkeys(
            ("quality", "accuracy", "policy", "platform_fit", "brand_voice"), 0.9
        ),
        "issues": list(issues),
        "warnings": [],
    }
    data.update(changes)
    return data


def review_issue(node: str, **changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "severity": "error",
        "platform": "instagram",
        "responsible_node": node,
        "message": f"{node} problem",
        "fix_hint": f"fix the {node}",
    }
    data.update(changes)
    return data


class PostLLM:
    """Answers each post-flow prompt by role, whatever the call order."""

    def __init__(self, fake_llm: FakeScript) -> None:
        self.fake = fake_llm
        self.caption: Any = lambda platform, n: {
            "caption": CAPTION,
            "title": None,
            "hook": "Green tea is an easy daily habit.",
            "cta": None,
            "language": "en",
            "refused": False,
            "reason": None,
        }
        self.image: Any = lambda platform, n: {
            "prompt": f"tea on a table for {platform}",
            "negative_prompt": None,
            "aspect_ratio": "4:5" if platform == "instagram" else "1:1",
            "alt_text": "A cup of green tea",
            "style_notes": "calm greens",
        }
        self.tags: Any = lambda platform, n: {"hashtags": TAGS[platform]}
        self.review: Any = lambda n: review_reply()
        self.seen: dict[str, int] = {}
        fake_llm.set_default(self._answer)

    def _answer(self, call: FakeCall) -> dict[str, Any]:
        system, user = call.messages[0].content, call.messages[1].content
        if "quality judge" in system:
            return JUDGE
        if "final reviewer" in system:
            self.seen["review"] = self.seen.get("review", 0) + 1
            return self.review(self.seen["review"])  # type: ignore[no-any-return]
        role = (
            "caption"
            if "copywriter" in system
            else "image"
            if "art director" in system
            else "tags"
        )
        platform = user.split("Platform: ")[1].split("\n")[0]
        n = self.seen[f"{role}:{platform}"] = self.seen.get(f"{role}:{platform}", 0) + 1
        return getattr(self, role)(platform, n)  # type: ignore[no-any-return]


class Run:
    """The post subgraph inside a parent graph with an in-memory checkpointer."""

    def __init__(self, **parts: Any) -> None:
        parent = StateGraph(GraphState)
        parent.add_node("post_subgraph", make_post_subgraph(**parts))
        parent.add_edge(START, "post_subgraph")
        parent.add_edge("post_subgraph", END)
        self.graph = parent.compile(checkpointer=InMemorySaver())
        self.config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    def run(self, initial: GraphState | None = None) -> dict[str, Any]:
        return self.graph.invoke(initial or state(), self.config)

    def resume(self, value: Any) -> dict[str, Any]:
        return self.graph.invoke(Command(resume=value), self.config)


def test_the_subgraph_produces_a_full_draft_per_platform(fake_llm: FakeScript) -> None:
    PostLLM(fake_llm)

    result = Run().run()

    assert set(result["drafts"]) == {"facebook", "instagram"}
    storage = get_storage_backend()
    for platform, size in (("facebook", (1024, 1024)), ("instagram", (1024, 1280))):
        draft = result["drafts"][platform]
        assert draft["caption"] == CAPTION
        assert draft["hashtags"] == TAGS[platform]
        (media,) = draft["media"]
        info = read_image_info(storage.get(media["storage_key"]))
        assert (info.width, info.height) == size  # type: ignore[union-attr]
        assert media["meta"]["alt_text"] == "A cup of green tea"
    assert not any(result["retries"].values())


def test_each_platform_is_generated_once_however_many_accounts_share_it(
    fake_llm: FakeScript, fake_image: FakeImage
) -> None:
    llm = PostLLM(fake_llm)

    Run().run()

    assert llm.seen == {
        "caption:facebook": 1,
        "caption:instagram": 1,
        "image:facebook": 1,
        "image:instagram": 1,
        "tags:facebook": 1,
        "tags:instagram": 1,
    }
    assert len(fake_image.calls) == 2  # three accounts, two images


def test_the_steps_run_in_order_writer_image_hashtag(fake_llm: FakeScript) -> None:
    PostLLM(fake_llm)

    Run().run()

    roles = []
    for call in fake_llm.calls:
        system = call.messages[0].content
        if "copywriter" in system:
            roles.append("writer")
        elif "art director" in system:
            roles.append("image")
        elif "hashtag specialist" in system:
            roles.append("hashtag")
    assert [r for i, r in enumerate(roles) if i == 0 or r != roles[i - 1]] == [
        "writer",
        "image",
        "hashtag",
    ]


def test_the_image_sees_the_final_caption_and_the_hashtags_see_it_too(
    fake_llm: FakeScript,
) -> None:
    PostLLM(fake_llm)

    Run().run()

    for call in fake_llm.calls:
        system, user = call.messages[0].content, call.messages[1].content
        if "art director" in system or "hashtag specialist" in system:
            assert CAPTION in user


def test_a_failing_writer_retries_inside_the_subgraph(fake_llm: FakeScript) -> None:
    llm = PostLLM(fake_llm)
    good = llm.caption
    llm.caption = lambda platform, n: (
        {**good(platform, n), "caption": "Too short"}
        if platform == "instagram" and n == 1
        else good(platform, n)
    )

    result = Run().run()

    assert llm.seen["caption:instagram"] == 2
    assert result["drafts"]["instagram"]["caption"] == CAPTION
    assert result["retries"]["writer"] == 1


def test_a_bad_image_is_retried_without_redoing_the_writer(
    fake_llm: FakeScript, fake_image: FakeImage
) -> None:
    llm = PostLLM(fake_llm)
    fake_image.queue(build_png(256, 256, (1, 2, 3)), build_png(256, 256, (1, 2, 3)))

    result = Run().run()

    assert llm.seen["caption:instagram"] == 1  # the caption is not rewritten
    assert llm.seen["image:instagram"] == 2
    assert result["retries"]["image"] == 1
    key = result["drafts"]["instagram"]["media"][0]["storage_key"]
    assert key.endswith("image-2.png")


def test_an_escalation_inside_the_subgraph_pauses_and_resumes_the_run(
    fake_llm: FakeScript,
) -> None:
    llm = PostLLM(fake_llm)
    llm.tags = lambda platform, n: {"hashtags": ["nope"] if n <= 3 else TAGS[platform]}
    run = Run(hashtag=make_hashtag_node(max_retries=3))

    paused = run.run()

    (interrupt,) = paused["__interrupt__"]
    assert interrupt.value["step"] == "hashtag"
    assert interrupt.value["reason"] == "retries_exhausted"

    done = run.resume({"action": "retry"})

    assert done["drafts"]["instagram"]["hashtags"] == TAGS["instagram"]
    assert "__interrupt__" not in done


def test_an_aborted_escalation_ends_the_subgraph_as_failed(
    fake_llm: FakeScript,
) -> None:
    llm = PostLLM(fake_llm)
    llm.tags = lambda platform, n: {"hashtags": ["nope"]}
    run = Run(hashtag=make_hashtag_node(max_retries=1))

    run.run()
    done = run.resume({"action": "abort"})

    assert done["status"] == "failed"
    assert "hashtag" in done["errors"][0]


def test_the_parts_can_be_replaced(fake_llm: FakeScript) -> None:
    PostLLM(fake_llm)
    run = Run(
        writer=make_writer_node(use_judge=False),
        image=make_image_node(use_judge=False),
        hashtag=make_hashtag_node(),
    )

    run.run()

    assert not any("quality judge" in c.messages[0].content for c in fake_llm.calls)


def test_a_refusal_in_the_writer_escalates_the_whole_subgraph(
    fake_llm: FakeScript,
) -> None:
    llm = PostLLM(fake_llm)
    llm.caption = lambda platform, n: {
        "caption": "",
        "title": None,
        "hook": "",
        "cta": None,
        "language": "en",
        "refused": True,
        "reason": "unsafe topic",
    }

    paused = Run().run()

    (interrupt,) = paused["__interrupt__"]
    assert interrupt.value["step"] == "writer"
    assert interrupt.value["reason"] == "refused"
    assert llm.seen.get("image:facebook") is None  # nothing after the writer ran


# --------------------------------------------------------------------------
# In the main graph: request -> plan -> research -> strategy -> router -> post
# --------------------------------------------------------------------------


def shared_replies() -> list[dict[str, Any]]:
    plan = {
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
    notes = {
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
    return [
        plan,
        JUDGE,
        {"queries": ["green tea benefits"]},
        notes,
        JUDGE,
        {p: strat(p) for p in ("facebook", "instagram")},
        JUDGE,
    ]


def test_the_main_graph_runs_from_the_request_to_the_drafts(
    fake_llm: FakeScript,
) -> None:
    fake_llm.queue(*shared_replies())
    PostLLM(fake_llm)
    search = FakeSearch()
    graph = compile_graph(
        InMemorySaver(),
        planner=make_planner_node(),
        researcher=make_researcher_node(search_provider=lambda: search),
        strategist=make_strategist_node(),
        post_subgraph=make_post_subgraph(),
    )
    initial = state()
    for key in ("plan", "research", "strategy"):
        del initial[key]  # type: ignore[misc]

    result = graph.invoke(initial, thread_config(str(uuid.uuid4())))

    assert "__interrupt__" not in result
    assert set(result["drafts"]) == {"facebook", "instagram"}
    for platform in ("facebook", "instagram"):
        draft = result["drafts"][platform]
        assert draft["caption"] and draft["hashtags"] and len(draft["media"]) == 1
    assert not any(result["retries"].values())


# --------------------------------------------------------------------------
# Re-entry routing (rerun_from, AGENT_SPECS 5.2) and the review loop
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rerun_from", "entry"),
    [
        (None, "writer"),
        ("writer", "writer"),
        ("planner", "writer"),
        ("strategist", "writer"),
        ("image", "image"),
        ("hashtag", "hashtag"),
    ],
)
def test_the_entry_step_follows_rerun_from(rerun_from: Any, entry: str) -> None:
    assert route_post_entry(state(rerun_from=rerun_from)) == entry


@pytest.mark.parametrize(
    ("rerun_from", "feedback", "expected"),
    [
        (None, None, "image"),
        ("planner", None, "image"),
        ("writer", None, "hashtag"),
        ("writer", {"image": None}, "hashtag"),
        ("writer", {"image": "Make it simpler."}, "image"),
    ],
)
def test_after_a_writer_re_entry_the_image_is_kept_unless_flagged(
    rerun_from: Any, feedback: Any, expected: str
) -> None:
    base = state(rerun_from=rerun_from)
    if feedback is not None:
        base["feedback"] = feedback

    assert route_after_writer(base) == expected


def test_an_image_only_re_entry_stops_after_the_image() -> None:
    assert route_after_image(state(rerun_from="image")) == "end"
    assert route_after_image(state(rerun_from="writer")) == "hashtag"
    assert route_after_image(state()) == "hashtag"


def full_state() -> GraphState:
    """A finished first pass: drafts with caption, hashtags and a stored image."""
    return state(
        drafts={
            p: {
                "caption": CAPTION,
                "hashtags": TAGS[p],
                "media": [
                    {
                        "type": "image",
                        "storage_key": f"requests/r1/{p}/image-1.png",
                        "meta": {"alt_text": "tea"},
                    }
                ],
            }
            for p in ("facebook", "instagram")
        }
    )


def roles(fake_llm: FakeScript) -> list[str]:
    names = {
        "copywriter": "writer",
        "art director": "image",
        "hashtag specialist": "hashtag",
    }
    return [
        next(v for k, v in names.items() if k in c.messages[0].content)
        for c in fake_llm.calls
        if any(k in c.messages[0].content for k in names)
    ]


def test_a_hashtag_re_entry_runs_only_the_hashtag_step(fake_llm: FakeScript) -> None:
    PostLLM(fake_llm)
    base = full_state()
    base["rerun_from"] = "hashtag"
    base["feedback"] = {"hashtag": "Use 5 tags."}

    result = Run().run(base)

    assert set(roles(fake_llm)) == {"hashtag"}
    assert "Use 5 tags." in fake_llm.calls[0].messages[1].content
    assert result["drafts"]["instagram"]["caption"] == CAPTION  # kept


def test_an_image_re_entry_runs_only_the_image_step(
    fake_llm: FakeScript, fake_image: FakeImage
) -> None:
    PostLLM(fake_llm)
    base = full_state()
    base["rerun_from"] = "image"
    base["feedback"] = {"image": "Make it simpler."}

    result = Run().run(base)

    assert set(roles(fake_llm)) == {"image"}
    assert len(fake_image.calls) == 2
    assert result["drafts"]["instagram"]["hashtags"] == TAGS["instagram"]  # kept
    assert result["drafts"]["instagram"]["media"][0]["storage_key"].endswith(
        "/image-1.png"
    )


def test_a_writer_re_entry_reruns_writer_and_hashtag_but_not_the_image(
    fake_llm: FakeScript, fake_image: FakeImage
) -> None:
    PostLLM(fake_llm)
    base = full_state()
    base["rerun_from"] = "writer"
    base["feedback"] = {"writer": "Shorten it."}

    Run().run(base)

    assert set(roles(fake_llm)) == {"writer", "hashtag"}
    assert fake_image.calls == []


def test_a_writer_re_entry_also_reruns_a_flagged_image(
    fake_llm: FakeScript, fake_image: FakeImage
) -> None:
    PostLLM(fake_llm)
    base = full_state()
    base["rerun_from"] = "writer"
    base["feedback"] = {"writer": "Shorten it.", "image": "Match the caption."}

    Run().run(base)

    assert set(roles(fake_llm)) == {"writer", "image", "hashtag"}
    assert len(fake_image.calls) == 2


def make_main(**parts: Any) -> tuple[Any, dict[str, Any]]:
    """The main graph with the real post subgraph and the reviewer."""
    search = FakeSearch()
    graph = compile_graph(
        InMemorySaver(),
        planner=make_planner_node(),
        researcher=make_researcher_node(search_provider=lambda: search),
        strategist=make_strategist_node(),
        post_subgraph=make_post_subgraph(),
        **parts,
    )
    return graph, thread_config(str(uuid.uuid4()))


def request_state() -> GraphState:
    initial = state()
    for key in ("plan", "research", "strategy"):
        del initial[key]  # type: ignore[misc]
    return initial


def test_the_review_loop_fixes_the_hashtags_and_then_approves(
    fake_llm: FakeScript,
) -> None:
    fake_llm.queue(*shared_replies())
    llm = PostLLM(fake_llm)
    llm.review = lambda n: (
        review_reply(review_issue("hashtag")) if n == 1 else review_reply()
    )
    graph, config = make_main()

    result = graph.invoke(request_state(), config)

    assert "__interrupt__" not in result
    assert llm.seen["review"] == 2
    assert llm.seen["tags:instagram"] == 2 and llm.seen["caption:instagram"] == 1
    assert result["review_cycles"] == 1 and result["rerun_from"] is None
    assert result["review"]["ok"] is True
    assert (
        "fix the hashtag"
        in [c for c in fake_llm.calls if "hashtag specialist" in c.messages[0].content][
            -1
        ]
        .messages[1]
        .content
    )


def test_a_writer_fix_reruns_writer_and_hashtag_only(
    fake_llm: FakeScript, fake_image: FakeImage
) -> None:
    fake_llm.queue(*shared_replies())
    llm = PostLLM(fake_llm)
    llm.review = lambda n: (
        review_reply(review_issue("writer")) if n == 1 else review_reply()
    )
    graph, config = make_main()

    graph.invoke(request_state(), config)

    assert llm.seen["caption:instagram"] == 2 and llm.seen["tags:instagram"] == 2
    assert llm.seen["image:instagram"] == 1 and len(fake_image.calls) == 2


def test_the_cycle_cap_continues_with_the_remaining_errors_as_warnings(
    fake_llm: FakeScript,
) -> None:
    fake_llm.queue(*shared_replies())
    llm = PostLLM(fake_llm)
    llm.review = lambda n: review_reply(review_issue("hashtag"))
    graph, config = make_main()

    result = graph.invoke(request_state(), config)

    assert llm.seen["review"] == 3  # the first review and two fix cycles
    assert result["review_cycles"] == 2 and result["rerun_from"] is None
    assert result["review"]["warnings"] == ["instagram: hashtag problem"]
    assert llm.seen["tags:instagram"] == 3


def test_a_strategist_fix_goes_back_through_the_strategist(
    fake_llm: FakeScript,
) -> None:
    fake_llm.queue(*shared_replies())
    llm = PostLLM(fake_llm)
    llm.review = lambda n: (
        review_reply(review_issue("strategist")) if n == 1 else review_reply()
    )
    # The second strategist run (and its judge) after the shared replies:
    graph, config = make_main()
    original = llm._answer

    def answer(call: FakeCall) -> dict[str, Any]:
        if "social media strategist" in call.messages[0].content:
            return {p: strat(p) for p in ("facebook", "instagram")}
        return original(call)

    fake_llm.set_default(answer)

    result = graph.invoke(request_state(), config)

    assert result["review_cycles"] == 1
    assert llm.seen["caption:instagram"] == 2  # the content flow ran again


def test_an_aborted_step_in_the_subgraph_skips_the_review(
    fake_llm: FakeScript,
) -> None:
    fake_llm.queue(*shared_replies())
    llm = PostLLM(fake_llm)
    llm.tags = lambda platform, n: {"hashtags": ["nope"]}
    graph, config = make_main()

    paused = graph.invoke(request_state(), config)
    assert paused["__interrupt__"][0].value["step"] == "hashtag"
    done = graph.invoke(Command(resume={"action": "abort"}), config)

    assert done["status"] == "failed"
    assert "review" not in llm.seen
