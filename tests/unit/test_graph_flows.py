"""Whole-graph flow tests (B2-13), offline.

Every test here runs the **real** main graph: ``validate_input`` -> planner ->
researcher -> strategist -> router -> real post subgraph -> reviewer -> review gate,
on an in-memory checkpointer, with the scripted fake LLM, fake search, fake image
provider and a temporary media directory. Nothing touches the network or a database.

The five scenarios of TASKS B2-13 are covered at graph level: the retry path, the
escalation path, the review fix loop, the router in both directions and drafts for
several platforms. Node-level behaviour stays in the node test files.
"""

import socket
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from app.agents.checked import StepRecord
from app.agents.fake_flow import post_flow_replies
from app.agents.graph import compile_graph, thread_config
from app.agents.llm import FakeCall, FakeScript
from app.agents.nodes.researcher import make_researcher_node
from app.agents.post import make_post_subgraph
from app.providers.image import FakeImage
from app.providers.search import FakeSearch
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

pytestmark = pytest.mark.usefixtures("media_root", "fake_image", "no_network")

TOPIC = "Benefits of green tea"
ROLE_MARKERS = (
    ("strict quality judge", "judge"),
    ("final reviewer", "reviewer"),
    ("content planner", "planner"),
    ("research assistant", "queries"),
    ("research analyst", "notes"),
    ("social media strategist", "strategist"),
    ("copywriter", "writer"),
    ("art director", "image"),
    ("hashtag specialist", "hashtag"),
)
PER_PLATFORM = frozenset({"writer", "image", "hashtag"})
HAPPY_STEPS = [
    ("planner", 1, "passed"),
    ("researcher", 1, "passed"),
    ("strategist", 1, "passed"),
    ("writer", 1, "passed"),
    ("image", 1, "passed"),
    ("hashtag", 1, "passed"),
    ("reviewer", 1, "passed"),
]


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any attempt to open a connection fail the test (TASKS: no network)."""

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a graph test tried to open a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


@dataclass
class Ctx:
    """What an override sees about the call it may rewrite."""

    role: str
    platform: str | None
    n: int  # 1 for the first call of this role (and platform), 2 for the next, ...
    reply: dict[str, Any]  # the normal, valid reply
    user: str  # the user message (carries the retry feedback block, if any)


Override = Callable[[Ctx], dict[str, Any] | None]


class Flow:
    """A scripted whole post run: the dev runner's valid replies plus overrides.

    ``override(role, fn)``: ``fn(ctx)`` returns a replacement reply, or ``None`` to
    keep the normal one. Roles: planner, queries, notes, strategist, writer, image,
    hashtag, reviewer (judge calls always pass).
    """

    def __init__(
        self,
        fake_llm: FakeScript,
        platforms: tuple[str, ...] = ("facebook", "instagram"),
    ) -> None:
        self.base = post_flow_replies(TOPIC, "en", sorted(platforms))
        self.overrides: dict[str, Override] = {}
        self.calls: Counter[tuple[str, str | None]] = Counter()
        self.prompts: list[tuple[str, str | None, str]] = []  # role, platform, user
        fake_llm.set_default(self._answer)

    def override(self, role: str, fn: Override) -> None:
        self.overrides[role] = fn

    def count(self, role: str, platform: str | None = None) -> int:
        if platform is None and role in PER_PLATFORM:
            return sum(n for (r, _), n in self.calls.items() if r == role)
        return self.calls[(role, platform)]

    def user_prompts(self, role: str, platform: str | None = None) -> list[str]:
        return [
            user
            for r, p, user in self.prompts
            if r == role and (platform is None or p == platform)
        ]

    def _answer(self, call: FakeCall) -> dict[str, Any]:
        system, user = call.messages[0].content, call.messages[-1].content
        role = next(name for marker, name in ROLE_MARKERS if marker in system)
        platform = None
        if role in PER_PLATFORM:
            platform = user.split("Platform: ")[1].split("\n")[0].strip()
        self.calls[(role, platform)] += 1
        self.prompts.append((role, platform, user))
        reply = self.base(call)
        fn = self.overrides.get(role)
        if fn is None:
            return reply
        changed = fn(Ctx(role, platform, self.calls[(role, platform)], reply, user))
        return reply if changed is None else changed


def request(
    platforms: tuple[str, ...] = ("facebook", "instagram"),
    content_type: str = "post",
) -> dict[str, Any]:
    return {
        "request_id": str(uuid.uuid4()),
        "workspace_id": "w1",
        "user_id": "u1",
        "topic": TOPIC,
        "content_type": content_type,
        "target_accounts": [
            {"account_id": f"acc-{platform}", "platform": platform}
            for platform in platforms
        ],
        "publish_at": "2026-12-01T10:00:00Z",
        "language": "en",
    }


class Run:
    """The whole graph, one request, one thread; keeps the step records."""

    def __init__(
        self,
        fake_llm: FakeScript,
        *,
        platforms: tuple[str, ...] = ("facebook", "instagram"),
        content_type: str = "post",
        **graph_parts: Any,
    ) -> None:
        self.flow = Flow(fake_llm, platforms)
        self.records: list[StepRecord] = []
        search = FakeSearch()
        self.graph = compile_graph(
            InMemorySaver(),
            researcher=make_researcher_node(search_provider=lambda: search),
            post_subgraph=make_post_subgraph(),
            **graph_parts,
        )
        self.state = request(platforms, content_type)
        self.config = thread_config(
            self.state["request_id"], step_recorder=self.records.append
        )

    def start(self) -> dict[str, Any]:
        return self.graph.invoke(self.state, self.config)

    def resume(self, action: str) -> dict[str, Any]:
        return self.graph.invoke(Command(resume={"action": action}), self.config)

    def steps(self) -> list[tuple[str, int, str]]:
        return [(r.node_name, r.attempt, r.verdict) for r in self.records]

    def values(self) -> dict[str, Any]:
        return dict(self.graph.get_state(self.config).values)


# ---------------------------------------------------------------------------
# Ways to break one step (each returns a reply that fails that step's rules)
# ---------------------------------------------------------------------------

SCORES = dict.fromkeys(
    ("quality", "accuracy", "policy", "platform_fit", "brand_voice"), 0.9
)


def one_key_message(ctx: Ctx) -> dict[str, Any]:  # planner: needs 2 to 5
    return {**ctx.reply, "key_messages": ["only one"]}


def short_caption(ctx: Ctx) -> dict[str, Any]:  # writer: far below the target
    return {**ctx.reply, "caption": "Too short."}


def long_caption(ctx: Ctx) -> dict[str, Any]:  # writer: far above the target
    return {**ctx.reply, "caption": "Green tea is a simple daily habit. " * 45}


def bad_hashtags(ctx: Ctx) -> dict[str, Any]:  # hashtag: not a hashtag at all
    return {"hashtags": ["nope"]}


def review_error(
    node: str,
    platform: str = "instagram",
    *,
    every_time: bool = False,
    **changes: Any,
) -> Override:
    """A reviewer that finds one error in the first review (or in every review)."""

    def reply(ctx: Ctx) -> dict[str, Any] | None:
        if ctx.n > 1 and not every_time:
            return None
        issue = {
            "severity": "error",
            "platform": platform,
            "responsible_node": node,
            "message": f"{node} problem",
            "fix_hint": f"fix the {node}",
        }
        return {
            "ok": False,
            "scores": SCORES,
            "issues": [issue],
            "warnings": [],
            **changes,
        }

    return reply


def fail_first(
    role: str, breaker: Callable[[Ctx], dict[str, Any]], attempts: int
) -> Override:
    """Break the first ``attempts`` calls of a role (per platform)."""

    def reply(ctx: Ctx) -> dict[str, Any] | None:
        return breaker(ctx) if ctx.n <= attempts else None

    return reply


def paused(result: dict[str, Any]) -> dict[str, Any]:
    (interrupt,) = result["__interrupt__"]
    return dict(interrupt.value)


def test_the_guard_really_blocks_network_connections() -> None:
    with socket.socket() as sock, pytest.raises(AssertionError, match="network"):
        sock.connect(("127.0.0.1", 9))


# ---------------------------------------------------------------------------
# The happy path: the baseline every scenario below departs from
# ---------------------------------------------------------------------------


def test_a_clean_run_goes_through_every_step_once_and_ends_with_an_ok_review(
    fake_llm: FakeScript, fake_image: FakeImage
) -> None:
    run = Run(fake_llm)

    result = run.start()

    assert "__interrupt__" not in result
    assert run.steps() == HAPPY_STEPS
    assert set(result["drafts"]) == {"facebook", "instagram"}
    assert result["review"]["ok"] is True
    assert result["review"]["issues"] == []
    assert not result.get("review_cycles")
    assert not any(result["retries"].values())
    assert len(fake_image.calls) == 2
    assert run.graph.get_state(run.config).next == ()


# ---------------------------------------------------------------------------
# 1. The retry path
# ---------------------------------------------------------------------------


def test_a_bad_plan_is_retried_and_the_second_prompt_carries_the_feedback(
    fake_llm: FakeScript,
) -> None:
    run = Run(fake_llm)
    run.flow.override("planner", fail_first("planner", one_key_message, 1))

    result = run.start()

    assert run.steps() == [
        ("planner", 1, "retried"),
        ("planner", 2, "passed"),
        *HAPPY_STEPS[1:],
    ]
    feedback = run.records[0].feedback
    assert feedback and run.records[1].feedback is None
    first, second = run.flow.user_prompts("planner")
    assert feedback not in first
    assert feedback in second
    assert result["retries"]["planner"] == 1
    assert result["feedback"]["planner"] is None  # cleared by the pass
    assert result["plan"]["key_messages"] != ["only one"]


def test_a_writer_that_fails_twice_passes_on_the_third_attempt(
    fake_llm: FakeScript,
) -> None:
    run = Run(fake_llm)
    run.flow.override("writer", fail_first("writer", short_caption, 2))

    result = run.start()

    writer = [s for s in run.steps() if s[0] == "writer"]
    assert writer == [
        ("writer", 1, "retried"),
        ("writer", 2, "retried"),
        ("writer", 3, "passed"),
    ]
    assert "Too short." not in result["drafts"]["facebook"]["caption"]
    assert run.flow.count("writer", "facebook") == 3
    assert run.flow.count("image") == 2  # the image ran once, after the writer passed
    assert result["review"]["ok"] is True


def test_each_retry_prompt_carries_the_feedback_of_the_attempt_before(
    fake_llm: FakeScript,
) -> None:
    run = Run(fake_llm)
    run.flow.override("writer", fail_first("writer", short_caption, 2))

    run.start()

    feedback = [r.feedback for r in run.records if r.node_name == "writer"]
    prompts = run.flow.user_prompts("writer", "facebook")
    assert feedback[0] and feedback[1] and feedback[2] is None
    assert feedback[0] not in prompts[0]
    assert feedback[0] in prompts[1]
    assert feedback[1] in prompts[2]


def test_a_retry_never_reruns_the_steps_before_it(fake_llm: FakeScript) -> None:
    run = Run(fake_llm)
    run.flow.override("hashtag", fail_first("hashtag", bad_hashtags, 2))

    run.start()

    for role in ("planner", "queries", "notes", "strategist"):
        assert run.flow.count(role) == 1, role
    assert run.flow.count("writer") == 2 and run.flow.count("image") == 2
    assert run.flow.count("hashtag", "facebook") == 3


def test_every_step_has_its_own_retry_budget(fake_llm: FakeScript) -> None:
    """Two retries in the planner and two in the writer: neither escalates."""
    run = Run(fake_llm)
    run.flow.override("planner", fail_first("planner", one_key_message, 2))
    run.flow.override("writer", fail_first("writer", short_caption, 2))

    result = run.start()

    assert "__interrupt__" not in result
    assert result["retries"]["planner"] == 2 and result["retries"]["writer"] == 2
    assert run.graph.get_state(run.config).next == ()
    assert result["review"]["ok"] is True


def test_a_failing_image_provider_is_a_retried_attempt_then_passes(
    fake_llm: FakeScript, fake_image: FakeImage
) -> None:
    run = Run(fake_llm)
    fake_image.fail_next(1)  # retriable provider error on the first call

    result = run.start()

    image = [s for s in run.steps() if s[0] == "image"]
    assert [a for _, a, _ in image] == [1, 2]
    assert image[0][2] == "error" and image[1][2] == "passed"
    assert set(result["drafts"]) == {"facebook", "instagram"}
    assert run.flow.count("writer") == 2  # not redone


# ---------------------------------------------------------------------------
# 2. The escalation path
# ---------------------------------------------------------------------------


def test_a_planner_that_never_passes_pauses_the_run_before_any_research(
    fake_llm: FakeScript,
) -> None:
    run = Run(fake_llm)
    run.flow.override("planner", fail_first("planner", one_key_message, 99))

    result = run.start()

    payload = paused(result)
    assert payload["type"] == "needs_attention"
    assert payload["step"] == "planner"
    assert payload["reason"] == "retries_exhausted"
    assert (payload["attempts"], payload["max_retries"]) == (3, 3)
    assert run.steps() == [
        ("planner", 1, "retried"),
        ("planner", 2, "retried"),
        ("planner", 3, "escalated"),
    ]
    assert run.graph.get_state(run.config).next == ("escalate_planner",)
    for role in ("queries", "notes", "strategist", "writer", "image", "hashtag"):
        assert run.flow.count(role) == 0, role


def test_resume_with_retry_after_an_escalation_runs_the_flow_to_the_review(
    fake_llm: FakeScript, fake_image: FakeImage
) -> None:
    run = Run(fake_llm)
    run.flow.override("planner", fail_first("planner", one_key_message, 3))
    run.start()

    result = run.resume("retry")

    assert "__interrupt__" not in result
    assert (
        run.steps()
        == [
            ("planner", 1, "retried"),
            ("planner", 2, "retried"),
            ("planner", 3, "escalated"),
            ("planner", 4, "passed"),  # the attempt number keeps counting up
            *HAPPY_STEPS[1:],
        ]
    )
    assert set(result["drafts"]) == {"facebook", "instagram"}
    assert result["review"]["ok"] is True
    assert result["retries"]["planner"] == 0  # reset by the human's retry
    assert len(fake_image.calls) == 2


def test_an_escalation_inside_the_post_subgraph_resumes_where_it_stopped(
    fake_llm: FakeScript,
) -> None:
    run = Run(fake_llm)
    run.flow.override("writer", fail_first("writer", short_caption, 3))

    payload = paused(run.start())
    assert payload["step"] == "writer" and payload["reason"] == "retries_exhausted"
    assert run.flow.count("image") == 0
    before = {r: run.flow.count(r) for r in ("planner", "queries", "strategist")}

    result = run.resume("retry")

    assert "__interrupt__" not in result
    assert {r: run.flow.count(r) for r in before} == before  # nothing redone
    assert run.flow.count("planner") == 1
    assert [s for s in run.steps() if s[0] == "writer"][-1] == ("writer", 4, "passed")
    assert result["review"]["ok"] is True
    assert set(result["drafts"]) == {"facebook", "instagram"}


def test_aborting_an_escalation_ends_the_run_as_failed_and_skips_the_rest(
    fake_llm: FakeScript, fake_image: FakeImage
) -> None:
    run = Run(fake_llm)
    run.flow.override("writer", fail_first("writer", short_caption, 99))
    run.start()

    result = run.resume("abort")

    assert "__interrupt__" not in result
    assert result["status"] == "failed"
    assert any("writer" in error for error in result["errors"])
    assert run.steps()[-1][0] == "writer" and run.steps()[-1][2] == "failed"
    assert run.flow.count("image") == 0 and run.flow.count("hashtag") == 0
    assert run.flow.count("reviewer") == 0
    assert fake_image.calls == []
    assert run.graph.get_state(run.config).next == ()


def test_a_non_retriable_provider_error_escalates_at_the_first_attempt(
    fake_llm: FakeScript, fake_image: FakeImage
) -> None:
    run = Run(fake_llm)
    fake_image.fail_next(1, retriable=False)

    payload = paused(run.start())

    assert payload["step"] == "image"
    assert payload["reason"] == "non_retriable_error"
    assert payload["attempts"] == 1
    assert [s for s in run.steps() if s[0] == "image"] == [("image", 1, "escalated")]
    assert run.flow.count("hashtag") == 0


def test_a_refusing_writer_escalates_at_once_with_its_reason(
    fake_llm: FakeScript,
) -> None:
    run = Run(fake_llm)
    run.flow.override(
        "writer",
        lambda ctx: {**ctx.reply, "refused": True, "reason": "unsafe topic"},
    )

    payload = paused(run.start())

    assert payload["step"] == "writer" and payload["reason"] == "refused"
    assert payload["attempts"] == 1
    assert run.flow.count("writer", "facebook") == 1
    assert payload["refused"] == "facebook: unsafe topic"


def test_an_unknown_resolution_asks_again_instead_of_failing(
    fake_llm: FakeScript,
) -> None:
    run = Run(fake_llm)
    run.flow.override("planner", fail_first("planner", one_key_message, 3))
    run.start()

    again = run.resume("maybe")

    assert paused(again)["error"] == "invalid_resolution"
    assert paused(again)["step"] == "planner"
    done = run.resume("retry")  # the same pause can still be resolved properly
    assert "__interrupt__" not in done and done["review"]["ok"] is True


# ---------------------------------------------------------------------------
# 3. The review fix loop
# ---------------------------------------------------------------------------


def test_a_review_error_sends_the_run_back_to_the_responsible_step_and_the_review(
    fake_llm: FakeScript,
) -> None:
    run = Run(fake_llm)
    run.flow.override("reviewer", review_error("hashtag"))

    result = run.start()

    assert run.steps() == [
        *HAPPY_STEPS[:-1],
        ("reviewer", 1, "passed"),  # a review that finds errors is a valid result
        ("hashtag", 2, "passed"),
        ("reviewer", 2, "passed"),
    ]
    assert result["review_cycles"] == 1
    assert result["rerun_from"] is None
    assert result["review"]["ok"] is True
    assert "fix the hashtag" in run.flow.user_prompts("hashtag")[-1]


def test_a_fix_run_keeps_everything_the_responsible_step_did_not_change(
    fake_llm: FakeScript, fake_image: FakeImage
) -> None:
    run = Run(fake_llm)
    run.flow.override("reviewer", review_error("hashtag"))

    result = run.start()

    assert run.flow.count("writer") == 2  # once per platform, not redone
    assert run.flow.count("image") == 2 and len(fake_image.calls) == 2
    for draft in result["drafts"].values():
        (media,) = draft["media"]
        assert media["storage_key"].endswith("image-1.png")
        assert draft["caption"] and draft["hashtags"]


def test_a_writer_fix_reruns_the_writer_and_the_hashtags_but_not_the_image(
    fake_llm: FakeScript, fake_image: FakeImage
) -> None:
    run = Run(fake_llm)
    run.flow.override("reviewer", review_error("writer"))

    run.start()

    assert [s[:2] for s in run.steps() if s[0] in ("writer", "hashtag", "image")] == [
        ("writer", 1),
        ("image", 1),
        ("hashtag", 1),
        ("writer", 2),
        ("hashtag", 2),
    ]
    assert len(fake_image.calls) == 2
    assert "fix the writer" in run.flow.user_prompts("writer", "instagram")[-1]


def test_a_step_rerun_by_the_review_gets_a_fresh_retry_budget(
    fake_llm: FakeScript,
) -> None:
    """One failed hashtag attempt before the review plus two after must not escalate."""
    run = Run(fake_llm)
    run.flow.override("reviewer", review_error("hashtag"))

    def hashtags(ctx: Ctx) -> dict[str, Any] | None:
        # attempt 1 fails; the pass is attempt 2; after the review, attempts 3 and 4
        # fail and attempt 5 passes. Without a reset 1 + 2 = 3 failures would escalate.
        return bad_hashtags(ctx) if ctx.n in (1, 3, 4) else None

    run.flow.override("hashtag", hashtags)

    result = run.start()

    assert "__interrupt__" not in result
    assert [a for n, a, _ in run.steps() if n == "hashtag"] == [1, 2, 3, 4, 5]
    assert result["review"]["ok"] is True


def test_the_cycle_cap_ends_the_loop_and_keeps_the_errors_as_warnings(
    fake_llm: FakeScript,
) -> None:
    run = Run(fake_llm, max_review_cycles=2)
    run.flow.override("reviewer", review_error("hashtag", every_time=True))

    result = run.start()

    assert result["review_cycles"] == 2 and result["rerun_from"] is None
    assert [s for s in run.steps() if s[0] == "reviewer"] == [
        ("reviewer", 1, "passed"),
        ("reviewer", 2, "passed"),
        ("reviewer", 3, "passed"),
    ]
    assert result["review"]["warnings"] == ["instagram: hashtag problem"]
    assert "__interrupt__" not in result  # carries on to approval with warnings


def test_an_unfixable_policy_problem_pauses_for_a_human_who_can_retry_the_review(
    fake_llm: FakeScript,
) -> None:
    run = Run(fake_llm)
    run.flow.override(
        "reviewer",
        review_error("writer", scores={**SCORES, "policy": 0.1}),
    )

    payload = paused(run.start())

    assert payload["step"] == "reviewer" and payload["reason"] == "refused"
    assert "policy" in str(payload["refused"])
    assert run.steps()[-1] == ("reviewer", 1, "escalated")
    assert run.flow.count("writer") == 2  # no fix run was started

    result = run.resume("retry")

    assert "__interrupt__" not in result
    assert run.steps()[-1] == ("reviewer", 2, "passed")
    assert result["review"]["ok"] is True


def test_a_planner_fix_runs_the_research_and_the_strategy_again(
    fake_llm: FakeScript,
) -> None:
    run = Run(fake_llm)
    run.flow.override("reviewer", review_error("planner"))

    result = run.start()

    assert [s[0] for s in run.steps()] == [
        "planner", "researcher", "strategist", "writer", "image", "hashtag", "reviewer",
        "planner", "researcher", "strategist", "writer", "image", "hashtag", "reviewer",
    ]  # fmt: skip
    assert result["review_cycles"] == 1 and result["review"]["ok"] is True


# ---------------------------------------------------------------------------
# 4. The router, in both directions
# ---------------------------------------------------------------------------


def video_strategy(ctx: Ctx) -> dict[str, Any]:
    reply = ctx.reply["tiktok"]
    reply.update(
        length_target=60,
        hashtag_count=3,
        video={
            "format": "short",
            "aspect_ratio": "9:16",
            "target_duration_s": 60,
            "scene_count": 4,
            "style": "bright and fast",
        },
    )
    return ctx.reply


def test_a_post_request_takes_the_post_branch(
    fake_llm: FakeScript, fake_image: FakeImage
) -> None:
    run = Run(fake_llm)

    result = run.start()

    assert run.flow.count("writer") == 2 and run.flow.count("hashtag") == 2
    assert len(fake_image.calls) == 2
    assert all(d["media"] for d in result["drafts"].values())
    assert all(s["video"] is None for s in result["strategy"].values())


def test_a_video_request_takes_the_video_branch_and_never_the_post_nodes(
    fake_llm: FakeScript, fake_image: FakeImage
) -> None:
    run = Run(fake_llm, platforms=("tiktok",), content_type="video")
    run.flow.override("strategist", video_strategy)

    result = run.start()

    assert "__interrupt__" not in result
    assert run.steps() == [
        ("planner", 1, "passed"),
        ("researcher", 1, "passed"),
        ("strategist", 1, "passed"),
        ("reviewer", 1, "passed"),
    ]
    for role in ("writer", "image", "hashtag"):
        assert run.flow.count(role) == 0, role
    assert fake_image.calls == []
    assert result["drafts"] == {}  # the placeholder until B4
    assert result["strategy"]["tiktok"]["video"]["scene_count"] == 4


def test_a_video_request_without_a_video_strategy_is_retried_by_the_strategist(
    fake_llm: FakeScript,
) -> None:
    """The router only runs after the strategist passes, so it needs a video plan."""
    run = Run(fake_llm, platforms=("tiktok",), content_type="video")
    run.flow.override(
        "strategist",
        lambda ctx: video_strategy(ctx) if ctx.n == 2 else None,
    )

    run.start()

    assert [s for s in run.steps() if s[0] == "strategist"] == [
        ("strategist", 1, "retried"),
        ("strategist", 2, "passed"),
    ]
    assert "add a video object" in str(run.records[2].feedback)


def test_a_post_for_a_platform_that_only_takes_video_never_reaches_the_router(
    fake_llm: FakeScript,
) -> None:
    run = Run(fake_llm, platforms=("facebook", "tiktok"))

    result = run.start()

    assert result["status"] == "failed"
    assert any("tiktok" in error for error in result["errors"])
    assert run.records == [] and fake_llm.calls == []  # no LLM call, no step


# ---------------------------------------------------------------------------
# 5. Drafts for several platforms
# ---------------------------------------------------------------------------


def test_each_platform_gets_its_own_draft_with_its_own_rules(
    fake_llm: FakeScript,
) -> None:
    run = Run(fake_llm)

    def strategies(ctx: Ctx) -> dict[str, Any]:
        ctx.reply["facebook"]["hashtag_count"] = 2  # facebook allows 0 to 5
        ctx.reply["instagram"]["hashtag_count"] = 10  # instagram needs 3 to 15
        return ctx.reply

    run.flow.override("strategist", strategies)

    result = run.start()

    drafts = result["drafts"]
    assert len(drafts["facebook"]["hashtags"]) == 2
    assert len(drafts["instagram"]["hashtags"]) == 10
    request_id = run.state["request_id"]
    for platform, draft in drafts.items():
        (media,) = draft["media"]
        assert media["storage_key"].startswith(f"requests/{request_id}/{platform}/")
        assert media["type"] == "image"


def test_several_accounts_on_one_platform_share_one_draft(
    fake_llm: FakeScript, fake_image: FakeImage
) -> None:
    run = Run(fake_llm)
    run.state["target_accounts"] += [
        {"account_id": "acc-instagram-2", "platform": "instagram"},
        {"account_id": "acc-instagram-3", "platform": "instagram"},
    ]

    result = run.start()

    assert set(result["drafts"]) == {"facebook", "instagram"}
    assert run.flow.count("writer", "instagram") == 1
    assert run.flow.count("writer", "facebook") == 1
    assert len(fake_image.calls) == 2  # four accounts, two images


def test_the_order_of_the_accounts_does_not_change_the_drafts(
    fake_llm: FakeScript,
) -> None:
    first = Run(fake_llm)
    first_result = first.start()
    second = Run(fake_llm)
    second.state["target_accounts"].reverse()

    second_result = second.start()

    def comparable(result: dict[str, Any]) -> dict[str, Any]:
        return {
            platform: {
                "caption": draft["caption"],
                "hashtags": draft["hashtags"],
                "alt": draft["media"][0]["meta"]["alt_text"],
            }
            for platform, draft in result["drafts"].items()
        }

    assert comparable(first_result) == comparable(second_result)


def test_a_platform_without_an_account_gets_no_draft_and_no_image(
    fake_llm: FakeScript, fake_image: FakeImage
) -> None:
    run = Run(fake_llm, platforms=("facebook",))

    result = run.start()

    assert set(result["drafts"]) == {"facebook"}
    assert run.flow.count("writer", "instagram") == 0
    assert len(fake_image.calls) == 1
    assert set(result["strategy"]) == {"facebook"}


def test_a_problem_on_one_platform_is_named_in_the_feedback_and_the_step_reruns(
    fake_llm: FakeScript,
) -> None:
    run = Run(fake_llm)

    def long_on_instagram(ctx: Ctx) -> dict[str, Any] | None:
        if ctx.platform == "instagram" and ctx.n == 1:
            return long_caption(ctx)
        return None

    run.flow.override("writer", long_on_instagram)

    result = run.start()

    feedback = run.records[3].feedback
    assert run.steps()[3:5] == [("writer", 1, "retried"), ("writer", 2, "passed")]
    assert feedback and "instagram" in feedback and "facebook" not in feedback
    assert feedback in run.flow.user_prompts("writer", "instagram")[-1]
    assert set(result["drafts"]) == {"facebook", "instagram"}
    assert len(result["drafts"]["instagram"]["caption"]) < 2200
    assert result["review"]["ok"] is True
