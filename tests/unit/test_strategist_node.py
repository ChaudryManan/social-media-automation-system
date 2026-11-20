"""Tests for the ``strategist`` node (B2-07), with the fake LLM provider."""

import copy
import json
import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from app.agents.checked import StepRecord, add_checked_node
from app.agents.llm import FakeScript, LLMOutputError
from app.agents.nodes.strategist import (
    STRATEGIST_RUBRIC,
    PlatformStrategy,
    StrategyReply,
    VideoStrategy,
    make_check_strategy,
    make_strategist_node,
    make_strategy_agent,
    make_strategy_judge,
)
from app.agents.platform_rules import DEFAULT_PLATFORM_RULES
from app.agents.prompts import load_prompt
from app.agents.state import GraphState
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "AGENT_SPECS.md"
PROMPT = ROOT / "backend" / "app" / "agents" / "prompts" / "strategist.md"


def video(**changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "format": "short",
        "aspect_ratio": "9:16",
        "target_duration_s": 30,
        "scene_count": 4,
        "style": "warm, bright kitchen shots",
    }
    data.update(changes)
    return data


def strat(platform: str = "instagram", **changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "platform": platform,
        "tone": "warm and simple",
        "structure": "hook line, 3 short paragraphs, CTA",
        "length_target": 600,
        "cta": "Try one cup tomorrow",
        "hashtag_count": 5,
        "media_notes": "bright photo of a tea cup",
        "posting_notes": "morning",
        "video": None,
    }
    data.update(changes)
    return data


def reply(*platforms: str, **changes: Any) -> dict[str, Any]:
    return {p: strat(p, **changes) for p in (platforms or ("instagram", "facebook"))}


def video_reply(*platforms: str, **video_changes: Any) -> dict[str, Any]:
    return {
        p: strat(p, length_target=30, video=video(**video_changes))
        for p in (platforms or ("instagram", "tiktok"))
    }


def good_judge(score: float = 0.9) -> dict[str, Any]:
    return {"ok": True, "score": score, "issues": [], "feedback": ""}


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
        "plan": {"angle": "Tea is a daily habit", "key_messages": ["Easy start"]},
        "research": {"facts": [], "grounded": False, "cautions": ["avoid statistics"]},
    }
    base.update(changes)
    return base  # type: ignore[return-value]


def video_state(**changes: Any) -> GraphState:
    return state(
        content_type="video",
        target_accounts=[
            {"account_id": "a1", "platform": "instagram"},
            {"account_id": "a2", "platform": "tiktok"},
        ],
        **changes,
    )


# --------------------------------------------------------------------------
# Models and the spec / prompt contract
# --------------------------------------------------------------------------


def spec_fields(class_name: str) -> set[str]:
    text = SPEC.read_text(encoding="utf-8")
    block = re.search(
        rf"class {class_name}\(BaseModel\):\n(.*?)(?:\n\n|```)", text, re.S
    )
    assert block, f"{class_name} is missing from AGENT_SPECS.md"
    return set(re.findall(r"^    (\w+):", block.group(1), re.M))


@pytest.mark.parametrize(
    ("model", "name"),
    [(PlatformStrategy, "PlatformStrategy"), (VideoStrategy, "VideoStrategy")],
)
def test_model_fields_equal_the_spec_classes(model: type, name: str) -> None:
    assert set(model.model_fields) == spec_fields(name)  # type: ignore[attr-defined]


def prompt_text() -> str:
    return PROMPT.read_text(encoding="utf-8")


def test_prompt_example_fields_equal_the_model_fields() -> None:
    example = json.loads(re.search(r"```json\n(.*?)```", prompt_text(), re.S).group(1))  # type: ignore[union-attr]

    assert list(example) == ["<platform>"]
    (entry,) = example.values()
    assert set(entry) == set(PlatformStrategy.model_fields)
    PlatformStrategy.model_validate(entry)  # the example is itself valid
    StrategyReply.model_validate(example)


def test_the_prompt_names_every_video_field() -> None:
    rules = prompt_text().split("# Rules")[1]

    for field in VideoStrategy.model_fields:
        assert f'"{field}"' in rules


def test_a_full_reply_round_trips() -> None:
    data = {"instagram": strat(video=video())}

    assert StrategyReply.model_validate(data).model_dump() == {
        "instagram": strat(video=video())
    }


def test_optional_parts_default_to_none() -> None:
    data = strat()
    del data["cta"], data["video"]

    parsed = PlatformStrategy.model_validate(data)

    assert parsed.cta is None and parsed.video is None


@pytest.mark.parametrize(
    "field",
    [
        "platform",
        "tone",
        "structure",
        "length_target",
        "hashtag_count",
        "media_notes",
        "posting_notes",
    ],
)
def test_the_core_fields_are_required(field: str) -> None:
    data = strat()
    del data[field]

    with pytest.raises(ValidationError):
        PlatformStrategy.model_validate(data)


@pytest.mark.parametrize("value", ["medium", "", None, "Short"])
def test_an_unknown_video_format_is_a_schema_error(value: Any) -> None:
    with pytest.raises(ValidationError):
        VideoStrategy.model_validate(video(format=value))


def test_the_limits_are_rule_checks_not_schema_errors() -> None:
    parsed = PlatformStrategy.model_validate(
        strat(length_target=10**9, hashtag_count=99, tone="")
    )

    assert parsed.length_target == 10**9 and parsed.tone == ""
    assert VideoStrategy.model_validate(video(scene_count=99)).scene_count == 99


def test_a_reply_must_be_an_object_keyed_by_platform() -> None:
    with pytest.raises(ValidationError):
        StrategyReply.model_validate([strat()])


# --------------------------------------------------------------------------
# strategy_agent
# --------------------------------------------------------------------------


def test_the_agent_stores_the_strategy_keyed_by_platform(fake_llm: FakeScript) -> None:
    fake_llm.queue(reply())

    out = make_strategy_agent()(state())

    assert out.update == {"strategy": reply()}
    assert out.prompt_version == load_prompt("strategist").version == "strategist.v1"
    assert out.summary == {
        "platforms": ["facebook", "instagram"],
        "content_type": "post",
    }
    (call,) = fake_llm.calls
    assert call.kind == "text" and call.temperature == 0.5


def test_the_prompt_gets_the_plan_research_and_brand_voice(
    fake_llm: FakeScript,
) -> None:
    fake_llm.queue(reply())

    make_strategy_agent()(state(language="ur", brand_voice="playful pirate"))

    system, user = fake_llm.calls[0].messages
    assert "Brand voice: playful pirate" in user.content
    assert "Output language: ur" in user.content
    assert "Content type: post" in user.content
    assert "Tea is a daily habit" in user.content  # plan
    assert "avoid statistics" in user.content  # research
    assert "Benefits of green tea" not in system.content
    assert "playful pirate" not in system.content


def test_platforms_are_distinct_sorted_and_only_their_rules_are_sent(
    fake_llm: FakeScript,
) -> None:
    fake_llm.queue(reply())

    make_strategy_agent()(state())

    user = fake_llm.calls[0].messages[1].content
    assert "Target platforms (one strategy each): facebook, instagram\n" in user
    assert '"caption_max": 2200' in user  # instagram rule
    assert "tiktok" not in user and "youtube" not in user


def test_missing_plan_and_research_are_sent_as_none(fake_llm: FakeScript) -> None:
    fake_llm.queue(reply())
    base = state()
    del base["plan"], base["research"]  # type: ignore[misc]

    make_strategy_agent()(base)

    user = fake_llm.calls[0].messages[1].content
    assert "Content plan:\nnone" in user and "Research notes:\nnone" in user


def test_the_rules_come_from_the_injected_source(fake_llm: FakeScript) -> None:
    fake_llm.queue(reply())
    custom = {"instagram": {"caption_max": 111}, "facebook": {"caption_max": 222}}

    make_strategy_agent(lambda: custom)(state())

    user = fake_llm.calls[0].messages[1].content
    assert '"caption_max": 111' in user and '"caption_max": 222' in user


def test_retry_feedback_reaches_the_prompt(fake_llm: FakeScript) -> None:
    fake_llm.queue(reply())

    make_strategy_agent()(state(feedback={"strategist": "Add facebook."}))

    assert "Add facebook." in fake_llm.calls[0].messages[1].content


def test_a_reply_that_is_not_a_strategy_object_raises_an_output_error(
    fake_llm: FakeScript,
) -> None:
    fake_llm.queue([1, 2], "nonsense")

    with pytest.raises(LLMOutputError):
        make_strategy_agent()(state())


# --------------------------------------------------------------------------
# Rule checks
# --------------------------------------------------------------------------


def codes(result: Any) -> list[str]:
    return [i.code for i in result.issues]


def run_check(strategy: Any, base: GraphState | None = None, **kwargs: Any) -> Any:
    base = base or state()
    base["strategy"] = strategy
    return make_check_strategy(**kwargs)(base)


def test_a_good_post_strategy_passes() -> None:
    assert run_check(reply()).ok is True


def test_a_good_video_strategy_passes() -> None:
    assert run_check(video_reply(), video_state()).ok is True


def test_a_missing_platform_fails() -> None:
    result = run_check({"instagram": strat("instagram")})

    assert codes(result) == ["platform_missing"]
    assert "facebook" in result.feedback


def test_a_platform_that_is_not_a_target_fails() -> None:
    result = run_check({**reply(), "tiktok": strat("tiktok")})

    assert codes(result) == ["platform_unexpected"]
    assert "tiktok" in result.feedback


def test_no_strategy_at_all_fails_instead_of_crashing() -> None:
    assert codes(make_check_strategy()(state())) == ["platform_missing"]


def test_the_platform_field_must_equal_the_key() -> None:
    result = run_check({**reply(), "facebook": strat("instagram")})

    assert codes(result) == ["platform_field_mismatch"]


def test_a_non_object_entry_counts_as_a_missing_strategy() -> None:
    result = run_check({"instagram": strat("instagram"), "facebook": "oops"})

    assert codes(result) == ["platform_missing"]
    assert "facebook" in result.feedback


@pytest.mark.parametrize("field", ["tone", "structure", "media_notes", "posting_notes"])
@pytest.mark.parametrize("value", ["", "   ", None])
def test_empty_text_fields_fail(field: str, value: Any) -> None:
    result = run_check(reply(**{field: value}))

    assert set(codes(result)) == {"field_empty"}
    assert field in result.feedback


@pytest.mark.parametrize(
    ("platform", "limit"), [("instagram", 2200), ("facebook", 63000)]
)
def test_length_at_the_limit_passes_and_over_it_fails(
    platform: str, limit: int
) -> None:
    assert run_check(
        {platform: strat(platform, length_target=limit)}, _only(platform)
    ).ok

    result = run_check(
        {platform: strat(platform, length_target=limit + 1)}, _only(platform)
    )

    assert codes(result) == ["length_over_limit"]
    assert str(limit) in result.feedback


def _only(platform: str, content_type: str = "post") -> GraphState:
    return state(
        content_type=content_type,
        target_accounts=[{"account_id": "a", "platform": platform}],
    )


@pytest.mark.parametrize("value", [0, -5, None, "long", True])
def test_an_invalid_length_fails(value: Any) -> None:
    result = run_check({"instagram": strat(length_target=value)}, _only("instagram"))

    assert codes(result) == ["length_invalid"]


def test_a_platform_without_a_length_limit_accepts_any_length() -> None:
    rules = {"instagram": {"hashtags_min": 0, "hashtags_max": 9}}

    assert run_check(
        {"instagram": strat(length_target=10**7)},
        _only("instagram"),
        platform_rules=lambda: rules,
    ).ok


def test_description_max_is_the_limit_when_there_is_no_caption_max() -> None:
    rules = {"instagram": {"description_max": 100}}

    result = run_check(
        {"instagram": strat(length_target=101, hashtag_count=0)},
        _only("instagram"),
        platform_rules=lambda: rules,
    )

    assert codes(result) == ["length_over_limit"]


@pytest.mark.parametrize(
    ("count", "ok"), [(2, False), (3, True), (15, True), (16, False)]
)
def test_instagram_hashtag_range(count: int, ok: bool) -> None:
    result = run_check({"instagram": strat(hashtag_count=count)}, _only("instagram"))

    assert result.ok is ok
    if not ok:
        assert codes(result) == ["hashtag_count_range"]
        assert "between 3 and 15" in result.feedback


@pytest.mark.parametrize("value", [-1, None, "five", True])
def test_an_invalid_hashtag_count_fails(value: Any) -> None:
    result = run_check({"instagram": strat(hashtag_count=value)}, _only("instagram"))

    assert codes(result) == ["hashtag_count_invalid"]


def test_a_platform_without_hashtag_limits_accepts_any_count() -> None:
    rules = {"instagram": {"caption_max": 2200}}

    assert run_check(
        {"instagram": strat(hashtag_count=40)},
        _only("instagram"),
        platform_rules=lambda: rules,
    ).ok


def test_a_platform_without_rules_has_no_bounds() -> None:
    assert run_check(
        {"instagram": strat(length_target=10**7, hashtag_count=40)},
        _only("instagram"),
        platform_rules=lambda: {},
    ).ok


def test_a_post_must_not_have_a_video_object() -> None:
    result = run_check(reply(video=video()))

    assert set(codes(result)) == {"video_not_allowed"}


def test_a_video_request_needs_the_video_object() -> None:
    result = run_check(
        {p: strat(p, length_target=30) for p in ("instagram", "tiktok")}, video_state()
    )

    assert set(codes(result)) == {"video_missing"}


def test_the_video_aspect_ratio_must_be_allowed_for_the_platform() -> None:
    result = run_check(video_reply(aspect_ratio="16:9"), video_state())

    assert set(codes(result)) == {"aspect_ratio_not_allowed"}
    assert "use one of 9:16" in result.feedback


def test_a_platform_without_video_aspect_rules_accepts_any_ratio() -> None:
    rules = {"tiktok": {"hashtags_min": 0, "hashtags_max": 9}}

    assert run_check(
        {"tiktok": strat("tiktok", length_target=30, video=video(aspect_ratio="2:1"))},
        _only("tiktok", "video"),
        platform_rules=lambda: rules,
    ).ok


@pytest.mark.parametrize(("seconds", "ok"), [(90, True), (91, False)])
def test_the_video_duration_limit_comes_from_the_platform(
    seconds: int, ok: bool
) -> None:
    only = _only("instagram", "video")
    result = run_check(
        {"instagram": strat(length_target=30, video=video(target_duration_s=seconds))},
        only,
    )

    assert result.ok is ok
    if not ok:
        assert codes(result) == ["duration_over_limit"]


@pytest.mark.parametrize("value", [0, None, "30s", True])
def test_an_invalid_duration_fails(value: Any) -> None:
    result = run_check(
        {"instagram": strat(length_target=30, video=video(target_duration_s=value))},
        _only("instagram", "video"),
    )

    assert codes(result) == ["duration_invalid"]


def test_the_video_length_target_is_in_seconds_and_limited() -> None:
    result = run_check(
        {"instagram": strat(length_target=91, video=video())},
        _only("instagram", "video"),
    )

    assert codes(result) == ["length_over_limit"]
    assert "seconds" in result.feedback


@pytest.mark.parametrize(
    ("count", "ok"), [(2, False), (3, True), (5, True), (6, False), (None, False)]
)
def test_scene_count_is_between_three_and_the_setting(count: Any, ok: bool) -> None:
    result = run_check(video_reply(scene_count=count), video_state())

    assert result.ok is ok
    if not ok:
        assert set(codes(result)) == {"scene_count_range"}
        assert "between 3 and 5" in result.feedback


def test_the_scene_limit_is_a_parameter() -> None:
    assert run_check(video_reply(scene_count=7), video_state(), max_scenes=8).ok
    assert not run_check(video_reply(scene_count=4), video_state(), max_scenes=3).ok


def test_the_scene_limit_cannot_be_below_the_minimum() -> None:
    with pytest.raises(ValueError, match="max_scenes"):
        make_check_strategy(max_scenes=2)


def test_the_video_needs_a_style_guide() -> None:
    result = run_check(video_reply(style="  "), video_state())

    assert set(codes(result)) == {"style_empty"}


def test_every_problem_is_reported_in_one_feedback() -> None:
    result = run_check(
        {
            "instagram": strat(length_target=5000, hashtag_count=1),
            "facebook": strat("facebook", tone=""),
        }
    )

    assert sorted(codes(result)) == [
        "field_empty",
        "hashtag_count_range",
        "length_over_limit",
    ]


def test_the_rules_are_read_on_every_run() -> None:
    rules = copy.deepcopy(DEFAULT_PLATFORM_RULES)
    check = make_check_strategy(lambda: rules)
    base = state(strategy=reply())

    assert check(base).ok
    rules["instagram"]["hashtags_max"] = 4  # an admin changed the setting

    assert codes(check(base)) == ["hashtag_count_range"]


def test_the_rules_do_not_call_the_llm(fake_llm: FakeScript) -> None:
    run_check(reply())

    assert fake_llm.calls == []


# --------------------------------------------------------------------------
# Judge
# --------------------------------------------------------------------------


def test_the_judge_sends_strategy_plan_and_rubric_to_the_fast_model(
    fake_llm: FakeScript,
) -> None:
    fake_llm.queue(good_judge(0.8))

    verdict = make_strategy_judge()(state(strategy=reply()))

    assert verdict.ok and verdict.score == 0.8
    call = fake_llm.calls[0]
    assert call.kind == "fast" and call.temperature == 0.0
    user = call.messages[1].content
    assert STRATEGIST_RUBRIC.splitlines()[0] in user
    assert "Step being judged: strategist" in user
    assert "Tea is a daily habit" in user
    assert "hook line, 3 short paragraphs, CTA" in user
    assert "Brand voice: warm and simple" in user


def test_the_judge_sees_only_the_target_platform_rules(fake_llm: FakeScript) -> None:
    fake_llm.queue(good_judge())

    make_strategy_judge()(state(strategy=reply()))

    user = fake_llm.calls[0].messages[1].content
    assert '"caption_max": 2200' in user
    assert "tiktok" not in user and "youtube" not in user


# --------------------------------------------------------------------------
# The checked node (agent + rules + judge + retry)
# --------------------------------------------------------------------------


class Flow:
    """START -> strategist (checked) -> END."""

    def __init__(self, **node_args: Any) -> None:
        self.steps: list[StepRecord] = []
        self.node = make_strategist_node(**node_args)
        builder = StateGraph(GraphState)
        entry = add_checked_node(builder, self.node, on_pass=END)
        builder.add_edge(START, entry)
        self.graph = builder.compile(checkpointer=InMemorySaver())
        self.config = {
            "configurable": {
                "thread_id": str(uuid.uuid4()),
                "step_recorder": self.steps.append,
            }
        }

    def run(self, initial: GraphState | None = None) -> dict[str, Any]:
        return self.graph.invoke(initial or state(), self.config)

    def resume(self, value: Any) -> dict[str, Any]:
        return self.graph.invoke(Command(resume=value), self.config)


def test_a_good_strategy_passes_at_once(fake_llm: FakeScript) -> None:
    fake_llm.queue(reply(), good_judge())
    flow = Flow()

    result = flow.run()

    assert set(result["strategy"]) == {"instagram", "facebook"}  # one per platform
    assert [c.kind for c in fake_llm.calls] == ["text", "fast"]
    (step,) = flow.steps
    assert (step.node_name, step.attempt, step.verdict) == ("strategist", 1, "passed")
    assert step.output_summary["prompt_version"] == "strategist.v1"
    assert step.output_summary["score"] == 0.9
    assert step.tokens_in > 0


def test_a_video_request_gets_video_strategies(fake_llm: FakeScript) -> None:
    fake_llm.queue(video_reply(), good_judge())

    result = Flow().run(video_state())

    assert result["strategy"]["tiktok"]["video"]["scene_count"] == 4


def test_a_rule_failure_retries_with_feedback_and_skips_the_judge(
    fake_llm: FakeScript,
) -> None:
    fake_llm.queue({"instagram": strat("instagram")}, reply(), good_judge())
    flow = Flow()

    result = flow.run()

    assert set(result["strategy"]) == {"instagram", "facebook"}
    assert [c.kind for c in fake_llm.calls] == ["text", "text", "fast"]
    assert "Add a strategy for: facebook." in fake_llm.calls[1].messages[1].content
    assert [s.verdict for s in flow.steps] == ["retried", "passed"]
    assert result["retries"] == {"strategist": 1}


def test_a_low_judge_score_retries_with_the_judge_feedback(
    fake_llm: FakeScript,
) -> None:
    low = {
        "ok": False,
        "score": 0.4,
        "issues": [
            {"code": "off_plan", "message": "Match the plan.", "severity": "error"}
        ],
        "feedback": "Match the plan. Keep: the tone",
    }
    fake_llm.queue(reply(), low, reply(), good_judge())
    flow = Flow()

    flow.run()

    assert "Match the plan. Keep: the tone" in fake_llm.calls[2].messages[1].content
    assert [s.verdict for s in flow.steps] == ["retried", "passed"]


def test_exhausted_retries_escalate_and_resume_retry_continues(
    fake_llm: FakeScript,
) -> None:
    bad = reply(hashtag_count=99)
    fake_llm.queue(bad, bad, bad, reply(), good_judge())
    flow = Flow()

    paused = flow.run()

    (interrupt,) = paused["__interrupt__"]
    assert interrupt.value["step"] == "strategist"
    assert interrupt.value["reason"] == "retries_exhausted"
    assert {i["code"] for i in interrupt.value["issues"]} == {"hashtag_count_range"}

    done = flow.resume({"action": "retry"})

    assert set(done["strategy"]) == {"instagram", "facebook"}


def test_the_retry_limit_and_scene_limit_are_parameters(fake_llm: FakeScript) -> None:
    bad = video_reply(scene_count=4)
    fake_llm.queue(bad, bad)
    flow = Flow(max_retries=2, max_scenes=3)

    paused = flow.run(video_state())

    assert paused["__interrupt__"][0].value["max_retries"] == 2
    assert [s.verdict for s in flow.steps] == ["retried", "escalated"]


def test_a_provider_outage_is_a_retried_error_attempt(fake_llm: FakeScript) -> None:
    fake_llm.fail_next(1)
    fake_llm.queue(reply(), good_judge())
    flow = Flow()

    result = flow.run()

    assert set(result["strategy"]) == {"instagram", "facebook"}
    assert [s.verdict for s in flow.steps] == ["error", "passed"]


def test_without_the_judge_only_the_rules_decide(fake_llm: FakeScript) -> None:
    fake_llm.queue(reply())

    result = Flow(use_judge=False).run()

    assert set(result["strategy"]) == {"instagram", "facebook"}
    assert [c.kind for c in fake_llm.calls] == ["text"]


def test_the_node_uses_the_documented_step_key_and_default_limits() -> None:
    node = make_strategist_node()

    assert node.key == "strategist"
    assert node.max_retries == 3
