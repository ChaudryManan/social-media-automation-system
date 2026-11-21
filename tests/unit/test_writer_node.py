"""Tests for the post-flow ``writer`` node (B2-10), with the fake LLM provider."""

import json
import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from app.agents.checked import StepRecord, add_checked_node
from app.agents.llm import FakeScript, LLMOutputError
from app.agents.platform_rules import DEFAULT_PLATFORM_RULES
from app.agents.post.writer import (
    WRITER_RUBRIC,
    WriterOutput,
    make_check_writer,
    make_writer_agent,
    make_writer_judge,
    make_writer_node,
)
from app.agents.prompts import load_prompt
from app.agents.state import GraphState
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "AGENT_SPECS.md"
PROMPT = ROOT / "backend" / "app" / "agents" / "prompts" / "writer.md"

LONG_ENOUGH = "Green tea is an easy daily habit. " * 17  # about 600 characters


def out(**changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "caption": LONG_ENOUGH.strip(),
        "title": None,
        "hook": "Green tea is an easy daily habit.",
        "cta": "Try one cup tomorrow.",
        "language": "en",
        "refused": False,
        "reason": None,
    }
    data.update(changes)
    return data


def good_judge(score: float = 0.9) -> dict[str, Any]:
    return {"ok": True, "score": score, "issues": [], "feedback": ""}


def strat(platform: str, **changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "platform": platform,
        "tone": "warm",
        "structure": "hook, short lines, CTA",
        "length_target": 600,
        "cta": "Try it",
        "hashtag_count": 5,
        "media_notes": "bright photo",
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
        "brand_voice": "warm and simple",
        "language": "en",
        "plan": {"angle": "Tea is a daily habit", "key_messages": ["Easy start"]},
        "research": {"facts": [], "grounded": False, "cautions": ["avoid statistics"]},
        "strategy": {"instagram": strat("instagram"), "facebook": strat("facebook")},
    }
    base.update(changes)
    return base  # type: ignore[return-value]


def drafted(**captions: str) -> GraphState:
    """A state whose drafts hold the given captions (default: a good one each)."""
    captions = captions or {
        "instagram": LONG_ENOUGH.strip(),
        "facebook": LONG_ENOUGH.strip(),
    }
    return state(drafts={p: {"caption": c} for p, c in captions.items()})


# --------------------------------------------------------------------------
# Model and the spec / prompt contract
# --------------------------------------------------------------------------


def test_model_fields_equal_the_spec_class() -> None:
    text = SPEC.read_text(encoding="utf-8")
    block = re.search(r"class WriterOutput\(BaseModel\):\n(.*?)\n```", text, re.S)
    assert block
    spec = set(re.findall(r"^    (\w+):", block.group(1), re.M))

    assert set(WriterOutput.model_fields) == spec


def test_prompt_example_fields_equal_the_model_fields() -> None:
    text = PROMPT.read_text(encoding="utf-8")
    example = json.loads(re.search(r"```json\n(.*?)```", text, re.S).group(1))  # type: ignore[union-attr]

    assert set(example) == set(WriterOutput.model_fields)
    WriterOutput.model_validate(example)  # the example is itself valid


def test_a_full_reply_round_trips_and_optional_parts_default() -> None:
    assert WriterOutput.model_validate(out()).model_dump() == out()
    data = out()
    for field in ("title", "cta", "refused", "reason"):
        del data[field]

    parsed = WriterOutput.model_validate(data)

    assert parsed.title is None and parsed.cta is None
    assert parsed.refused is False and parsed.reason is None


@pytest.mark.parametrize("field", ["caption", "hook", "language"])
def test_the_core_fields_are_required(field: str) -> None:
    data = out()
    del data[field]

    with pytest.raises(ValidationError):
        WriterOutput.model_validate(data)


def test_limits_are_rule_checks_not_schema_errors() -> None:
    parsed = WriterOutput.model_validate(out(caption="x" * 10**6, hook=""))

    assert len(parsed.caption) == 10**6 and parsed.hook == ""


def test_text_is_trimmed() -> None:
    assert WriterOutput.model_validate(out(caption="  hi  \n")).caption == "hi"


# --------------------------------------------------------------------------
# writer_agent
# --------------------------------------------------------------------------


def test_the_agent_writes_one_caption_per_distinct_platform(
    fake_llm: FakeScript,
) -> None:
    fake_llm.queue(out(caption="Facebook text"), out(caption="Instagram text"))

    result = make_writer_agent()(state())

    assert result.update == {
        "drafts": {
            "facebook": {"caption": "Facebook text"},
            "instagram": {"caption": "Instagram text"},
        }
    }
    # Three accounts, two platforms: two calls, never one per account (FR-5.5).
    assert len(fake_llm.calls) == 2
    assert all(c.kind == "text" and c.temperature == 0.8 for c in fake_llm.calls)
    assert result.prompt_version == load_prompt("writer").version == "writer.v1"
    assert result.summary == {
        "platforms": ["facebook", "instagram"],
        "languages": {"facebook": "en", "instagram": "en"},
        "caption_chars": {"facebook": 13, "instagram": 14},
    }
    assert result.refused_reason is None


def test_the_prompt_gets_that_platforms_inputs(fake_llm: FakeScript) -> None:
    fake_llm.queue(out(), out())
    base = state(
        language="ur",
        brand_voice="playful pirate",
        strategy={
            "instagram": strat("instagram", tone="insta tone"),
            "facebook": strat("facebook", tone="fb tone"),
        },
    )

    make_writer_agent(banned_words=lambda: ["foo", "bar"])(base)

    facebook, instagram = (c.messages for c in fake_llm.calls)
    fb_user, ig_user = facebook[1].content, instagram[1].content
    assert "Platform: facebook" in fb_user and "Platform: instagram" in ig_user
    assert "fb tone" in fb_user and "insta tone" not in fb_user
    assert "insta tone" in ig_user and "fb tone" not in ig_user
    assert '"caption_max": 63000' in fb_user and '"caption_max": 2200' in ig_user
    assert "Output language: ur" in ig_user
    assert "Brand voice: playful pirate" in ig_user
    assert "Banned words: foo, bar" in ig_user
    assert "Tea is a daily habit" in ig_user  # plan
    assert "avoid statistics" in ig_user  # research
    assert "Benefits of green tea" not in instagram[0].content  # topic stays out


def test_missing_banned_words_plan_and_research_are_sent_as_none(
    fake_llm: FakeScript,
) -> None:
    fake_llm.queue(out(), out())
    base = state()
    del base["plan"], base["research"]  # type: ignore[misc]

    make_writer_agent()(base)

    user = fake_llm.calls[0].messages[1].content
    assert "Banned words: none" in user
    assert "Content plan:\nnone" in user and "Research notes:\nnone" in user


def test_the_rules_come_from_the_injected_source(fake_llm: FakeScript) -> None:
    fake_llm.queue(out(), out())
    custom = {"instagram": {"caption_max": 111}, "facebook": {"caption_max": 222}}

    make_writer_agent(lambda: custom)(state())

    users = [c.messages[1].content for c in fake_llm.calls]
    assert '"caption_max": 222' in users[0] and '"caption_max": 111' in users[1]


def test_a_platform_without_rules_gets_none(fake_llm: FakeScript) -> None:
    fake_llm.queue(out(), out())

    make_writer_agent(lambda: {})(state())

    assert "Platform rules:\nnone" in fake_llm.calls[0].messages[1].content


def test_retry_feedback_reaches_every_platform_prompt(fake_llm: FakeScript) -> None:
    fake_llm.queue(out(), out())

    make_writer_agent()(state(feedback={"writer": "instagram: shorten it."}))

    for call in fake_llm.calls:
        assert "instagram: shorten it." in call.messages[1].content


def test_other_draft_fields_are_kept_and_a_stale_title_is_dropped(
    fake_llm: FakeScript,
) -> None:
    fake_llm.queue(out(caption="new fb"), out(caption="new ig"))
    old = {
        "facebook": {
            "caption": "old",
            "title": "stale",
            "hashtags": ["#a"],
            "media": [{"type": "image", "storage_key": "k"}],
        },
        "instagram": {"caption": "old ig", "hashtags": ["#b"]},
    }

    result = make_writer_agent()(state(drafts=old))

    assert result.update["drafts"] == {
        "facebook": {
            "caption": "new fb",
            "hashtags": ["#a"],
            "media": [{"type": "image", "storage_key": "k"}],
        },
        "instagram": {"caption": "new ig", "hashtags": ["#b"]},
    }
    assert "title" in old["facebook"]  # the input state is not mutated


def test_a_youtube_title_is_stored(fake_llm: FakeScript) -> None:
    fake_llm.queue(out(title="Green tea in 3 steps"))
    base = state(
        target_accounts=[{"account_id": "a1", "platform": "youtube"}],
        strategy={"youtube": strat("youtube")},
    )

    result = make_writer_agent()(base)

    assert result.update["drafts"]["youtube"]["title"] == "Green tea in 3 steps"


def test_a_refusal_escalates_with_the_platform_and_writes_nothing(
    fake_llm: FakeScript,
) -> None:
    fake_llm.queue(out(), out(caption="", hook="", refused=True, reason="unsafe"))

    result = make_writer_agent()(state())

    assert result.refused_reason == "instagram: unsafe"
    assert result.update == {}


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (None, "facebook: the topic cannot be written safely"),
        ("  ", "facebook: the topic cannot be written safely"),
        ("r" * 500, "facebook: " + "r" * 199 + "…"),
    ],
)
def test_the_refusal_reason_has_a_default_and_a_limit(
    fake_llm: FakeScript, reason: str | None, expected: str
) -> None:
    fake_llm.queue(out(caption="", hook="", refused=True, reason=reason))

    assert make_writer_agent()(state()).refused_reason == expected


def test_a_missing_strategy_is_a_bug_not_a_retry(fake_llm: FakeScript) -> None:
    base = state(strategy={"instagram": strat("instagram")})

    with pytest.raises(ValueError, match="facebook"):
        make_writer_agent()(base)
    assert fake_llm.calls == []


def test_an_invalid_reply_raises_an_output_error(fake_llm: FakeScript) -> None:
    fake_llm.queue([1, 2], "nonsense")

    with pytest.raises(LLMOutputError):
        make_writer_agent()(state())


# --------------------------------------------------------------------------
# Rule checks
# --------------------------------------------------------------------------


def codes(result: Any) -> list[str]:
    return [i.code for i in result.issues]


def run_check(base: GraphState | None = None, **kwargs: Any) -> Any:
    return make_check_writer(**kwargs)(base or drafted())


def with_captions(**captions: str) -> GraphState:
    return drafted(**captions)


def test_good_captions_pass() -> None:
    assert run_check().ok is True


def test_a_missing_or_blank_caption_fails() -> None:
    result = run_check(state(drafts={"instagram": {"caption": "  "}}))

    assert codes(result) == ["caption_empty", "caption_empty"]
    assert "instagram: write the caption." in result.feedback


def test_no_drafts_at_all_fails_instead_of_crashing() -> None:
    assert codes(run_check(state())) == ["caption_empty", "caption_empty"]


def test_a_caption_that_is_not_text_fails() -> None:
    result = run_check(state(drafts={"instagram": {"caption": 5}}))

    assert "caption_empty" in codes(result)


@pytest.mark.parametrize(("size", "ok"), [(2200, True), (2201, False)])
def test_the_platform_length_limit(size: int, ok: bool) -> None:
    rules = {"instagram": {"caption_max": 2200}, "facebook": {}}
    base = state(
        strategy={"instagram": strat("instagram", length_target=size)},
        drafts={"instagram": {"caption": "x" * size}},
        target_accounts=[{"account_id": "a1", "platform": "instagram"}],
    )

    result = make_check_writer(lambda: rules)(base)

    assert result.ok is ok
    if not ok:
        assert codes(result) == ["caption_too_long"]
        assert "2201 characters but the limit is 2200" in result.feedback


def test_description_max_is_the_limit_when_there_is_no_caption_max() -> None:
    rules = {"youtube": {"description_max": 50}}
    base = state(
        target_accounts=[{"account_id": "a1", "platform": "youtube"}],
        strategy={"youtube": strat("youtube", length_target=51)},
        drafts={"youtube": {"caption": "x" * 51, "title": "A title"}},
    )

    assert codes(make_check_writer(lambda: rules)(base)) == ["caption_too_long"]


def test_a_platform_without_a_limit_accepts_any_length() -> None:
    base = state(
        strategy={"instagram": strat("instagram", length_target=5000)},
        drafts={"instagram": {"caption": "x" * 5000}},
        target_accounts=[{"account_id": "a1", "platform": "instagram"}],
    )

    assert make_check_writer(lambda: {})(base).ok is True


@pytest.mark.parametrize(
    ("length", "ok"),
    [(419, False), (420, True), (600, True), (780, True), (781, False)],
)
def test_the_caption_stays_within_30_percent_of_the_target(
    length: int, ok: bool
) -> None:
    result = run_check(with_captions(instagram="x" * length, facebook="x" * 600))

    assert result.ok is ok
    if not ok:
        assert codes(result) == ["caption_off_target"]
        assert "about 600 (420 to 780)" in result.feedback


def test_the_off_target_message_says_which_way_to_go() -> None:
    short = run_check(with_captions(instagram="x" * 100, facebook="x" * 600))
    long = run_check(with_captions(instagram="x" * 1000, facebook="x" * 600))

    assert "Lengthen it." in short.feedback
    assert "Shorten it." in long.feedback


@pytest.mark.parametrize("target", [None, 0, -5, "600", True])
def test_a_missing_or_invalid_length_target_is_not_checked(target: Any) -> None:
    base = drafted(instagram="x" * 50, facebook="x" * 50)
    base["strategy"] = {
        "instagram": strat("instagram", length_target=target),
        "facebook": strat("facebook", length_target=target),
    }

    assert run_check(base).ok is True


def test_a_missing_strategy_entry_is_not_checked_for_length() -> None:
    base = drafted()
    base["strategy"] = {}

    assert run_check(base).ok is True


@pytest.mark.parametrize(
    "text",
    [
        "Start #green today " + "x" * 580,
        "#tea first " + "x" * 580,
        "Great #Té habit " + "x" * 580,
        "tea\n#tea " + "x" * 580,
    ],
)
def test_hashtags_in_the_caption_fail(text: str) -> None:
    result = run_check(with_captions(instagram=text, facebook=LONG_ENOUGH.strip()))

    assert codes(result) == ["hashtag_in_caption"]
    assert "another step adds them" in result.feedback


@pytest.mark.parametrize(
    "text",
    [
        "The #1 habit " + "x" * 580,  # a number sign before a digit
        "Learn C# " + "x" * 580,  # after a word character
        "Use R&#39;s " + "x" * 580,  # an HTML entity
        "Section a#b " + "x" * 580,
        "Tag # alone " + "x" * 580,
    ],
)
def test_a_number_sign_that_is_not_a_hashtag_passes(text: str) -> None:
    assert run_check(with_captions(instagram=text, facebook="x" * 600)).ok is True


@pytest.mark.parametrize(
    "text",
    [
        "Read more at https://example.com today " + "x" * 560,
        "Visit http://example.com " + "x" * 570,
        "Go to www.example.com now " + "x" * 570,
        "HTTPS://EXAMPLE.COM " + "x" * 580,
    ],
)
def test_raw_urls_fail(text: str) -> None:
    result = run_check(with_captions(instagram=text, facebook="x" * 600))

    assert codes(result) == ["raw_url"]


@pytest.mark.parametrize(
    "text",
    [
        "Hello [brand] friends " + "x" * 580,
        "Hello {{brand}} friends " + "x" * 580,
        "Hello {brand} friends " + "x" * 580,
        "Only closing }} " + "x" * 580,
    ],
)
def test_unfilled_placeholders_fail(text: str) -> None:
    result = run_check(with_captions(instagram=text, facebook="x" * 600))

    assert codes(result) == ["placeholder_left"]


def test_brackets_around_long_text_or_across_lines_are_not_placeholders() -> None:
    text = "Note [" + "y" * 41 + "] " + "x" * 560
    multi = "Item [one\ntwo] " + "x" * 580

    assert run_check(with_captions(instagram=text, facebook="x" * 600)).ok is True
    assert run_check(with_captions(instagram=multi, facebook="x" * 600)).ok is True


def test_banned_words_fail_without_naming_the_word() -> None:
    base = with_captions(
        instagram="Try the forbidden brew " + "x" * 570, facebook="x" * 600
    )

    result = run_check(base, banned_words=lambda: ["forbidden"])

    assert codes(result) == ["banned_word"]
    assert "forbidden" not in result.feedback
    assert "instagram" in result.feedback


def test_banned_words_match_whole_words_only() -> None:
    base = with_captions(instagram="A classy habit " + "x" * 580, facebook="x" * 600)

    assert run_check(base, banned_words=lambda: ["ass"]).ok is True


def test_a_banned_word_in_the_title_fails() -> None:
    rules = {"youtube": {"title_max": 100}}
    base = state(
        target_accounts=[{"account_id": "a1", "platform": "youtube"}],
        strategy={"youtube": strat("youtube", length_target=10)},
        drafts={"youtube": {"caption": "x" * 10, "title": "A forbidden title"}},
    )

    result = make_check_writer(lambda: rules, lambda: ["forbidden"])(base)

    assert codes(result) == ["banned_word"]


def youtube_state(**draft: Any) -> GraphState:
    return state(
        target_accounts=[{"account_id": "a1", "platform": "youtube"}],
        strategy={"youtube": strat("youtube", length_target=10)},
        drafts={"youtube": {"caption": "x" * 10, **draft}},
    )


def test_youtube_needs_a_title_within_its_limit() -> None:
    rules = {"youtube": {"title_max": 20}}
    check = make_check_writer(lambda: rules)

    assert check(youtube_state(title="A fine title")).ok is True
    assert codes(check(youtube_state())) == ["title_missing"]
    assert codes(check(youtube_state(title="  "))) == ["title_missing"]
    long = check(youtube_state(title="t" * 21))
    assert codes(long) == ["title_too_long"]
    assert "21 characters but the limit is 20" in long.feedback
    assert check(youtube_state(title="t" * 20)).ok is True


def test_youtube_without_a_title_limit_accepts_any_title() -> None:
    assert make_check_writer(lambda: {})(youtube_state(title="t" * 500)).ok is True


def test_only_youtube_may_have_a_title() -> None:
    base = drafted()
    base["drafts"]["instagram"]["title"] = "A title"  # type: ignore[index]

    result = run_check(base)

    assert codes(result) == ["title_not_allowed"]
    assert "instagram: leave the title empty" in result.feedback


def test_every_problem_is_reported_in_one_feedback() -> None:
    base = with_captions(instagram="#tag https://x.io " + "x" * 10, facebook="")

    result = run_check(base)

    assert set(codes(result)) == {
        "hashtag_in_caption",
        "raw_url",
        "caption_off_target",
        "caption_empty",
    }
    assert "instagram" in result.feedback and "facebook" in result.feedback


def test_the_rules_and_banned_words_are_read_on_every_run() -> None:
    rules = {"instagram": {"caption_max": 10_000}}
    banned: list[str] = []
    check = make_check_writer(lambda: rules, lambda: banned)
    base = with_captions(instagram="x" * 600, facebook="x" * 600)

    assert check(base).ok is True
    rules["instagram"]["caption_max"] = 100
    assert codes(check(base)) == ["caption_too_long"]
    banned.append("x" * 600)  # the whole 600-character word in both captions
    assert set(codes(check(base))) == {"caption_too_long", "banned_word"}


def test_the_rules_do_not_call_the_llm(fake_llm: FakeScript) -> None:
    run_check()

    assert fake_llm.calls == []


# --------------------------------------------------------------------------
# Judge
# --------------------------------------------------------------------------


def test_the_judge_sends_captions_rubric_and_research_to_the_fast_model(
    fake_llm: FakeScript,
) -> None:
    fake_llm.queue(good_judge())
    base = drafted()
    base["research"] = {"facts": [{"claim": "Tea is widely drunk"}], "grounded": True}

    result = make_writer_judge()(base)

    assert result.ok is True and result.score == 0.9
    (call,) = fake_llm.calls
    assert call.kind == "fast" and call.temperature == 0.0
    user = call.messages[1].content
    assert "Step being judged: writer" in user
    assert WRITER_RUBRIC.splitlines()[0] in user
    assert "Tea is widely drunk" in user  # research is part of the rubric
    assert LONG_ENOUGH.strip() in user  # the captions are the output
    assert "Brand voice: warm and simple" in user


def test_the_judge_sees_titles_only_when_there_is_one_and_only_target_rules(
    fake_llm: FakeScript,
) -> None:
    fake_llm.queue(good_judge())
    base = youtube_state(title="A fine title")

    make_writer_judge()(base)

    user = fake_llm.calls[0].messages[1].content
    assert "A fine title" in user
    assert '"title_max": 100' in user
    assert "instagram" not in user and "tiktok" not in user


def test_the_judge_handles_missing_research_and_plan(fake_llm: FakeScript) -> None:
    fake_llm.queue(good_judge())
    base = drafted()
    del base["research"], base["plan"]  # type: ignore[misc]

    make_writer_judge()(base)

    assert "Research notes (the only source of specific facts):\nnone" in (
        fake_llm.calls[0].messages[1].content
    )


# --------------------------------------------------------------------------
# The checked node (agent + rules + judge + retry)
# --------------------------------------------------------------------------


class Flow:
    """START -> writer (checked) -> END."""

    def __init__(self, **node_args: Any) -> None:
        self.steps: list[StepRecord] = []
        self.node = make_writer_node(**node_args)
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


def test_good_captions_pass_at_once(fake_llm: FakeScript) -> None:
    fake_llm.queue(out(), out(), good_judge())
    flow = Flow()

    result = flow.run()

    assert set(result["drafts"]) == {"facebook", "instagram"}
    assert [c.kind for c in fake_llm.calls] == ["text", "text", "fast"]
    (step,) = flow.steps
    assert (step.node_name, step.attempt, step.verdict) == ("writer", 1, "passed")
    assert step.output_summary["prompt_version"] == "writer.v1"
    assert step.output_summary["score"] == 0.9


def test_a_rule_failure_retries_with_feedback_that_changes_the_second_attempt(
    fake_llm: FakeScript,
) -> None:
    fake_llm.queue(
        out(),
        out(caption="Too short"),  # attempt 1: facebook fine, instagram short
        out(),
        out(),  # attempt 2: both fine
        good_judge(),
    )
    flow = Flow()

    result = flow.run()

    first, second = fake_llm.calls[1], fake_llm.calls[3]
    assert "Previous attempt feedback" not in first.messages[1].content
    assert "Previous attempt feedback" in second.messages[1].content
    assert "instagram: the caption has 9 characters" in second.messages[1].content
    assert result["drafts"]["instagram"]["caption"] == LONG_ENOUGH.strip()
    assert [c.kind for c in fake_llm.calls] == ["text"] * 4 + ["fast"]  # judge skipped
    assert [s.verdict for s in flow.steps] == ["retried", "passed"]
    assert result["retries"] == {"writer": 1}


def test_a_low_judge_score_retries_with_the_judge_feedback(
    fake_llm: FakeScript,
) -> None:
    low = {
        "ok": False,
        "score": 0.4,
        "issues": [
            {"code": "weak_hook", "message": "Open stronger.", "severity": "error"}
        ],
        "feedback": "Open stronger. Keep: the tone",
    }
    fake_llm.queue(out(), out(), low, out(), out(), good_judge())
    flow = Flow()

    flow.run()

    assert "Open stronger. Keep: the tone" in fake_llm.calls[4].messages[1].content
    assert [s.verdict for s in flow.steps] == ["retried", "passed"]


def test_exhausted_retries_escalate_and_resume_retry_continues(
    fake_llm: FakeScript,
) -> None:
    bad = out(caption="short")
    fake_llm.queue(*[bad] * 6, out(), out(), good_judge())
    flow = Flow()

    paused = flow.run()

    (interrupt,) = paused["__interrupt__"]
    assert interrupt.value["step"] == "writer"
    assert interrupt.value["reason"] == "retries_exhausted"
    assert {i["code"] for i in interrupt.value["issues"]} == {"caption_off_target"}

    done = flow.resume({"action": "retry"})

    assert set(done["drafts"]) == {"facebook", "instagram"}


def test_a_refusal_escalates_at_once_without_retrying(fake_llm: FakeScript) -> None:
    fake_llm.queue(out(caption="", hook="", refused=True, reason="not safe"))
    flow = Flow()

    paused = flow.run()

    (interrupt,) = paused["__interrupt__"]
    assert interrupt.value["reason"] == "refused"
    assert interrupt.value["refused"] == "facebook: not safe"
    assert [s.verdict for s in flow.steps] == ["escalated"]
    assert len(fake_llm.calls) == 1


def test_a_provider_outage_is_a_retried_error_attempt(fake_llm: FakeScript) -> None:
    fake_llm.fail_next(1)
    fake_llm.queue(out(), out(), good_judge())
    flow = Flow()

    result = flow.run()

    assert set(result["drafts"]) == {"facebook", "instagram"}
    assert [s.verdict for s in flow.steps] == ["error", "passed"]


def test_without_the_judge_only_the_rules_decide(fake_llm: FakeScript) -> None:
    fake_llm.queue(out(), out())

    Flow(use_judge=False).run()

    assert [c.kind for c in fake_llm.calls] == ["text", "text"]


def test_the_banned_words_hook_reaches_prompt_and_check(fake_llm: FakeScript) -> None:
    fake_llm.queue(out(), out(), good_judge())
    flow = Flow(banned_words=lambda: ["xyzzy"])

    flow.run()

    assert "Banned words: xyzzy" in fake_llm.calls[0].messages[1].content


def test_the_retry_limit_and_threshold_are_parameters(fake_llm: FakeScript) -> None:
    fake_llm.queue(*[out(caption="short")] * 2)
    flow = Flow(max_retries=1)

    paused = flow.run()

    assert paused["__interrupt__"][0].value["max_retries"] == 1
    assert [s.verdict for s in flow.steps] == ["escalated"]


def test_the_node_uses_the_documented_step_key_and_default_limits() -> None:
    node = make_writer_node()

    assert node.key == "writer"
    assert node.max_retries == 3
    assert DEFAULT_PLATFORM_RULES["instagram"]["caption_max"] == 2200
