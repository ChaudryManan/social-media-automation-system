"""Tests for the post-flow ``hashtag`` node (B2-10), with the fake LLM provider."""

import json
import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from app.agents.checked import StepRecord, add_checked_node
from app.agents.llm import FakeScript, LLMOutputError
from app.agents.post.hashtag import (
    HashtagOutput,
    make_check_hashtag,
    make_hashtag_agent,
    make_hashtag_node,
)
from app.agents.prompts import load_prompt
from app.agents.state import GraphState
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "AGENT_SPECS.md"
PROMPT = ROOT / "backend" / "app" / "agents" / "prompts" / "hashtag.md"

IG_TAGS = ["#greentea", "#tea", "#morningroutine", "#wellness", "#habits"]
FB_TAGS = ["#greentea", "#tea"]


def reply(*tags: str) -> dict[str, Any]:
    return {"hashtags": list(tags)}


def strat(platform: str, **changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "platform": platform,
        "tone": "warm",
        "structure": "hook",
        "length_target": 600,
        "cta": None,
        "hashtag_count": 5,
        "media_notes": "photo",
        "posting_notes": "morning",
        "video": None,
    }
    data.update(changes)
    return data


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
        "language": "en",
        "plan": {"angle": "Tea is a daily habit"},
        "strategy": {
            "instagram": strat("instagram", hashtag_count=5),
            "facebook": strat("facebook", hashtag_count=2),
        },
        "drafts": {
            "instagram": {"caption": "Instagram caption", "media": [{"type": "image"}]},
            "facebook": {"caption": "Facebook caption"},
        },
    }
    base.update(changes)
    return base  # type: ignore[return-value]


def tagged(instagram: Any = IG_TAGS, facebook: Any = FB_TAGS) -> GraphState:
    base = state()
    base["drafts"]["instagram"]["hashtags"] = instagram  # type: ignore[index]
    base["drafts"]["facebook"]["hashtags"] = facebook  # type: ignore[index]
    return base


# --------------------------------------------------------------------------
# Model and the spec / prompt contract
# --------------------------------------------------------------------------


def test_model_fields_equal_the_spec() -> None:
    text = SPEC.read_text(encoding="utf-8")
    assert "HashtagOutput(hashtags: list[str])" in text

    assert set(HashtagOutput.model_fields) == {"hashtags"}


def test_prompt_example_fields_equal_the_model_fields() -> None:
    text = PROMPT.read_text(encoding="utf-8")
    example = json.loads(re.search(r"```json\n(.*?)```", text, re.S).group(1))  # type: ignore[union-attr]

    assert set(example) == set(HashtagOutput.model_fields)
    HashtagOutput.model_validate(example)


def test_the_reply_needs_a_list_of_strings_and_trims_each_tag() -> None:
    assert HashtagOutput.model_validate({"hashtags": [" #a ", "#b"]}).hashtags == [
        "#a",
        "#b",
    ]
    with pytest.raises(ValidationError):
        HashtagOutput.model_validate({})
    with pytest.raises(ValidationError):
        HashtagOutput.model_validate({"hashtags": "#a"})


# --------------------------------------------------------------------------
# hashtag_agent
# --------------------------------------------------------------------------


def test_the_agent_adds_hashtags_per_distinct_platform_and_keeps_the_rest(
    fake_llm: FakeScript,
) -> None:
    fake_llm.queue(reply(*FB_TAGS), reply(*IG_TAGS))

    result = make_hashtag_agent()(state())

    drafts = result.update["drafts"]
    assert drafts["facebook"] == {"caption": "Facebook caption", "hashtags": FB_TAGS}
    assert drafts["instagram"] == {
        "caption": "Instagram caption",
        "media": [{"type": "image"}],
        "hashtags": IG_TAGS,
    }
    assert len(fake_llm.calls) == 2  # three accounts, two platforms (FR-5.5)
    assert all(c.kind == "text" and c.temperature == 0.5 for c in fake_llm.calls)
    assert result.prompt_version == load_prompt("hashtag").version == "hashtag.v1"
    assert result.summary == {
        "platforms": ["facebook", "instagram"],
        "hashtag_counts": {"facebook": 2, "instagram": 5},
    }


def test_the_prompt_gets_the_caption_count_and_platform_limits(
    fake_llm: FakeScript,
) -> None:
    fake_llm.queue(reply(*FB_TAGS), reply(*IG_TAGS))

    make_hashtag_agent(banned_words=lambda: ["spam"])(state(language="ur"))

    fb, ig = (c.messages[1].content for c in fake_llm.calls)
    assert "Platform: facebook" in fb and "Platform: instagram" in ig
    assert "Number of hashtags wanted: 2" in fb and "wanted: 5" in ig
    assert "Platform minimum: 0" in fb and "Platform maximum: 5" in fb
    assert "Platform minimum: 3" in ig and "Platform maximum: 15" in ig
    assert "Facebook caption" in fb and "Instagram caption" not in fb
    assert "Banned words: spam" in ig and "Output language: ur" in ig
    assert "Tea is a daily habit" in ig  # plan


def test_a_platform_without_hashtag_limits_sends_none(fake_llm: FakeScript) -> None:
    fake_llm.queue(reply(), reply())

    make_hashtag_agent(lambda: {})(state())

    user = fake_llm.calls[0].messages[1].content
    assert "Platform minimum: none" in user and "Platform maximum: none" in user


def test_retry_feedback_reaches_every_platform_prompt(fake_llm: FakeScript) -> None:
    fake_llm.queue(reply(), reply())

    make_hashtag_agent()(state(feedback={"hashtag": "Use fewer tags."}))

    for call in fake_llm.calls:
        assert "Use fewer tags." in call.messages[1].content


@pytest.mark.parametrize("caption", [None, ""])
def test_a_missing_caption_is_a_bug_not_a_retry(
    fake_llm: FakeScript, caption: str | None
) -> None:
    base = state()
    base["drafts"]["facebook"]["caption"] = caption  # type: ignore[index]

    with pytest.raises(ValueError, match="facebook caption"):
        make_hashtag_agent()(base)
    assert fake_llm.calls == []


def test_a_missing_strategy_is_a_bug_not_a_retry(fake_llm: FakeScript) -> None:
    base = state(strategy={"instagram": strat("instagram")})

    with pytest.raises(ValueError, match="facebook"):
        make_hashtag_agent()(base)


def test_an_invalid_reply_raises_an_output_error(fake_llm: FakeScript) -> None:
    fake_llm.queue({"tags": []}, "nonsense")

    with pytest.raises(LLMOutputError):
        make_hashtag_agent()(state())


# --------------------------------------------------------------------------
# Rule checks
# --------------------------------------------------------------------------


def codes(result: Any) -> list[str]:
    return [i.code for i in result.issues]


def test_good_hashtags_pass() -> None:
    assert make_check_hashtag()(tagged()).ok is True


def test_missing_hashtags_fail() -> None:
    result = make_check_hashtag()(state())

    assert codes(result) == ["hashtags_missing", "hashtags_missing"]
    assert "instagram: add the hashtags." in result.feedback


def test_hashtags_that_are_not_a_list_fail() -> None:
    result = make_check_hashtag()(tagged(instagram="#a #b #c"))

    assert codes(result) == ["hashtags_missing"]


@pytest.mark.parametrize(
    ("count", "ok"), [(2, False), (3, True), (15, True), (16, False)]
)
def test_the_instagram_range_is_three_to_fifteen(count: int, ok: bool) -> None:
    tags = [f"#tag{i}" for i in range(count)]

    result = make_check_hashtag()(tagged(instagram=tags))

    assert result.ok is ok
    if not ok:
        assert codes(result) == ["hashtag_count_range"]
        assert f"not {count}" in result.feedback


def test_zero_hashtags_are_fine_for_facebook() -> None:
    assert make_check_hashtag()(tagged(facebook=[])).ok is True


def test_facebook_allows_at_most_five() -> None:
    result = make_check_hashtag()(tagged(facebook=[f"#t{i}" for i in range(6)]))

    assert codes(result) == ["hashtag_count_range"]
    assert "between 0 and 5" in result.feedback


def test_a_platform_without_limits_accepts_any_count() -> None:
    check = make_check_hashtag(lambda: {})

    assert check(tagged(instagram=[], facebook=[f"#t{i}" for i in range(40)])).ok


def test_a_platform_with_only_a_minimum_or_maximum() -> None:
    only_min = make_check_hashtag(lambda: {"instagram": {"hashtags_min": 2}})
    only_max = make_check_hashtag(lambda: {"instagram": {"hashtags_max": 2}})

    assert only_min(tagged(instagram=["#a"])).ok is False
    assert (
        "between 2 and the platform maximum"
        in only_min(tagged(instagram=["#a"])).feedback
    )
    assert only_min(tagged(instagram=["#a", "#b", "#c"])).ok is True
    assert "between 0 and 2" in only_max(tagged(instagram=["#a", "#c", "#d"])).feedback


@pytest.mark.parametrize(
    "tag",
    [
        "tea",
        "#",
        "# tea",
        "#tea time",
        "#tea!",
        "#tea-time",
        "#tea.",
        "##tea",
        "",
        5,
        None,
    ],
)
def test_malformed_hashtags_fail(tag: Any) -> None:
    result = make_check_hashtag()(tagged(facebook=["#ok", tag]))

    assert codes(result) == ["hashtag_invalid"]
    assert "start with #" in result.feedback


@pytest.mark.parametrize("tag", ["#tea", "#Tea_Time", "#چائے", "#té", "#2026", "#a1"])
def test_letters_digits_and_underscores_in_any_script_are_fine(tag: str) -> None:
    assert make_check_hashtag()(tagged(facebook=["#ok", tag])).ok is True


@pytest.mark.parametrize(("size", "ok"), [(40, True), (41, False)])
def test_a_hashtag_has_at_most_forty_characters_including_the_hash(
    size: int, ok: bool
) -> None:
    tag = "#" + "a" * (size - 1)

    result = make_check_hashtag()(tagged(facebook=["#ok", tag]))

    assert result.ok is ok
    if not ok:
        assert codes(result) == ["hashtag_too_long"]


def test_duplicates_are_found_ignoring_case() -> None:
    result = make_check_hashtag()(tagged(facebook=["#Tea", "#tea"]))

    assert codes(result) == ["hashtag_duplicate"]
    assert "#tea" in result.feedback


def test_long_lists_of_bad_tags_name_only_a_few() -> None:
    tags = [f"bad{i}" for i in range(9)]

    result = make_check_hashtag()(tagged(facebook=tags[:5]))
    many = make_check_hashtag(lambda: {})(tagged(facebook=tags))

    assert "..." not in result.feedback
    assert "bad0, bad1, bad2, bad3, bad4, ..." in many.feedback
    assert "bad5" not in many.feedback


def test_a_banned_word_as_a_hashtag_fails_without_naming_it() -> None:
    result = make_check_hashtag(banned_words=lambda: ["spam"])(
        tagged(facebook=["#ok", "#spam"])
    )

    assert codes(result) == ["banned_word"]
    assert "spam" not in result.feedback


def test_a_banned_word_inside_a_longer_tag_is_not_a_whole_word() -> None:
    result = make_check_hashtag(banned_words=lambda: ["spam"])(
        tagged(facebook=["#ok", "#spammer"])
    )

    assert result.ok is True


def test_every_problem_is_reported_in_one_feedback() -> None:
    result = make_check_hashtag()(tagged(instagram=["#a", "#A"], facebook=["x"]))

    assert set(codes(result)) == {
        "hashtag_duplicate",
        "hashtag_count_range",
        "hashtag_invalid",
    }


def test_the_rules_are_read_on_every_run() -> None:
    rules = {"instagram": {"hashtags_max": 10}}
    check = make_check_hashtag(lambda: rules)
    base = tagged(facebook=[])

    assert check(base).ok is True
    rules["instagram"]["hashtags_max"] = 2
    assert codes(check(base)) == ["hashtag_count_range"]


def test_the_rules_do_not_call_the_llm(fake_llm: FakeScript) -> None:
    make_check_hashtag()(tagged())

    assert fake_llm.calls == []


# --------------------------------------------------------------------------
# The checked node (rule-only: no judge)
# --------------------------------------------------------------------------


class Flow:
    def __init__(self, **node_args: Any) -> None:
        self.steps: list[StepRecord] = []
        self.node = make_hashtag_node(**node_args)
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


def test_good_hashtags_pass_at_once_and_there_is_no_judge_call(
    fake_llm: FakeScript,
) -> None:
    fake_llm.queue(reply(*FB_TAGS), reply(*IG_TAGS))
    flow = Flow()

    result = flow.run()

    assert result["drafts"]["instagram"]["hashtags"] == IG_TAGS
    assert [c.kind for c in fake_llm.calls] == ["text", "text"]
    (step,) = flow.steps
    assert (step.node_name, step.attempt, step.verdict) == ("hashtag", 1, "passed")
    assert step.output_summary["prompt_version"] == "hashtag.v1"
    assert step.output_summary["score"] is None


def test_a_rule_failure_retries_with_feedback_that_changes_the_second_attempt(
    fake_llm: FakeScript,
) -> None:
    fake_llm.queue(
        reply(*FB_TAGS),
        reply("#a", "#A"),  # attempt 1: instagram has a duplicate
        reply(*FB_TAGS),
        reply(*IG_TAGS),  # attempt 2
    )
    flow = Flow()

    result = flow.run()

    second = fake_llm.calls[3].messages[1].content
    assert "Previous attempt feedback" not in fake_llm.calls[1].messages[1].content
    assert "instagram: remove the repeated hashtags" in second
    assert "instagram: give between 3 and 15" in second
    assert result["drafts"]["instagram"]["hashtags"] == IG_TAGS
    assert [s.verdict for s in flow.steps] == ["retried", "passed"]


def test_exhausted_retries_escalate_and_resume_retry_continues(
    fake_llm: FakeScript,
) -> None:
    bad = reply("nope")
    fake_llm.queue(*[bad] * 6, reply(*FB_TAGS), reply(*IG_TAGS))
    flow = Flow()

    paused = flow.run()

    (interrupt,) = paused["__interrupt__"]
    assert interrupt.value["step"] == "hashtag"
    assert interrupt.value["reason"] == "retries_exhausted"

    done = flow.resume({"action": "retry"})

    assert done["drafts"]["facebook"]["hashtags"] == FB_TAGS


def test_the_banned_words_hook_reaches_prompt_and_check(fake_llm: FakeScript) -> None:
    fake_llm.queue(reply("#ok", "#spam"), reply(*IG_TAGS))
    flow = Flow(banned_words=lambda: ["spam"], max_retries=1)

    paused = flow.run()

    assert "Banned words: spam" in fake_llm.calls[0].messages[1].content
    assert {i["code"] for i in paused["__interrupt__"][0].value["issues"]} == {
        "banned_word"
    }


def test_the_node_uses_the_documented_step_key_and_default_limits() -> None:
    node = make_hashtag_node()

    assert node.key == "hashtag"
    assert node.max_retries == 3
