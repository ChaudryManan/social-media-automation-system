"""Tests for the ``reviewer`` node, the review gate and the routing (B2-11)."""

import json
import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from app.agents.checked import StepRecord, add_checked_node
from app.agents.llm import FakeScript
from app.agents.nodes.reviewer import (
    DEFAULT_MAX_REVIEW_CYCLES,
    SCORE_KEYS,
    ReviewIssue,
    ReviewResult,
    earliest_node,
    is_valid_node,
    make_check_review,
    make_review_gate,
    make_reviewer_agent,
    make_reviewer_node,
    nodes_to_rerun,
    route_after_content,
    route_after_review,
    rule_issues,
    valid_nodes,
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
PROMPT = ROOT / "backend" / "app" / "agents" / "prompts" / "reviewer.md"

CAPTION = ("Green tea is an easy daily habit. " * 17).strip()
TAGS = ["#greentea", "#tea", "#morning", "#wellness", "#habits"]


def scores(**changes: float) -> dict[str, float]:
    data = dict.fromkeys(SCORE_KEYS, 0.9)
    data.update(changes)
    return data


def issue(node: str = "writer", **changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "severity": "error",
        "platform": "instagram",
        "responsible_node": node,
        "message": f"{node} is wrong",
        "fix_hint": f"fix the {node}",
    }
    data.update(changes)
    return data


def reply(*issues: dict[str, Any], **changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "ok": not any(i["severity"] == "error" for i in issues),
        "scores": scores(),
        "issues": list(issues),
        "warnings": [],
    }
    data.update(changes)
    return data


def draft(**changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "caption": CAPTION,
        "hashtags": TAGS,
        "media": [
            {
                "type": "image",
                "storage_key": "requests/r1/instagram/image-1.png",
                "meta": {
                    "prompt": "tea on a table",
                    "alt_text": "A cup of tea",
                    "aspect_ratio": "4:5",
                    "width": 1024,
                    "height": 1280,
                    "provider": "fake",
                },
            }
        ],
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
            {"account_id": "a2", "platform": "instagram"},
        ],
        "publish_at": "2026-12-01T10:00:00Z",
        "brand_voice": "warm and simple",
        "language": "en",
        "plan": {"angle": "Tea is a daily habit"},
        "research": {"facts": [], "grounded": False, "cautions": ["avoid statistics"]},
        "strategy": {"instagram": {"platform": "instagram", "hashtag_count": 5}},
        "drafts": {"instagram": draft()},
    }
    base.update(changes)
    return base  # type: ignore[return-value]


def video_state(**changes: Any) -> GraphState:
    return state(content_type="video", **changes)


def codes(result: Any) -> list[str]:
    return [i.code for i in result.issues]


# --------------------------------------------------------------------------
# Models and the spec / prompt contract
# --------------------------------------------------------------------------


def spec_fields(class_name: str) -> set[str]:
    text = SPEC.read_text(encoding="utf-8")
    block = re.search(rf"class {class_name}\(BaseModel\):\n(.*?)\n\n", text, re.S)
    assert block, class_name
    return set(re.findall(r"^    (\w+):", block.group(1), re.M))


@pytest.mark.parametrize(
    ("model", "name"), [(ReviewIssue, "ReviewIssue"), (ReviewResult, "ReviewResult")]
)
def test_model_fields_equal_the_spec_classes(model: type, name: str) -> None:
    assert set(model.model_fields) == spec_fields(name)  # type: ignore[attr-defined]


def test_prompt_example_fields_equal_the_model_fields() -> None:
    text = PROMPT.read_text(encoding="utf-8")
    example = json.loads(re.search(r"```json\n(.*?)```", text, re.S).group(1))  # type: ignore[union-attr]

    assert set(example) == set(ReviewResult.model_fields)
    assert set(example["issues"][0]) == set(ReviewIssue.model_fields)
    assert set(example["scores"]) == set(SCORE_KEYS)


def test_a_full_reply_round_trips_and_platform_is_optional() -> None:
    parsed = ReviewResult.model_validate(reply(issue()))

    assert parsed.model_dump() == reply(issue())
    data = issue()
    del data["platform"]
    assert ReviewIssue.model_validate(data).platform is None


@pytest.mark.parametrize("severity", ["fatal", "", None])
def test_an_unknown_severity_is_a_schema_error(severity: Any) -> None:
    with pytest.raises(ValidationError):
        ReviewIssue.model_validate(issue(severity=severity))


def test_the_node_names_and_scores_are_rule_checks_not_schema_errors() -> None:
    parsed = ReviewResult.model_validate(
        reply(issue("nonsense"), scores={"quality": 7})
    )

    assert parsed.issues[0].responsible_node == "nonsense"


# --------------------------------------------------------------------------
# Flow order and the re-entry table
# --------------------------------------------------------------------------


def test_valid_nodes_per_content_type() -> None:
    assert valid_nodes("post") == (
        "planner",
        "strategist",
        "writer",
        "image",
        "hashtag",
    )
    assert valid_nodes("video") == (
        "planner",
        "strategist",
        "script",
        "storyboard",
        "assembly",
        "voice",
    )


@pytest.mark.parametrize(
    ("node", "content_type", "ok"),
    [
        ("writer", "post", True),
        ("hashtag", "post", True),
        ("script", "post", False),
        ("scene:2", "post", False),
        ("scene:2", "video", True),
        ("scene:12", "video", True),
        ("scene:0", "video", False),
        ("scene:x", "video", False),
        ("scene:", "video", False),
        ("writer", "video", False),
        ("researcher", "post", False),
        ("", "post", False),
        (None, "post", False),
        (3, "video", False),
    ],
)
def test_is_valid_node(node: Any, content_type: str, ok: bool) -> None:
    assert is_valid_node(node, content_type) is ok


@pytest.mark.parametrize(
    ("nodes", "first"),
    [
        (["hashtag", "image", "writer"], "writer"),
        (["image", "hashtag"], "image"),
        (["strategist", "writer"], "strategist"),
        (["writer", "planner"], "planner"),
        (["voice", "scene:3", "scene:2"], "scene:2"),
        (["assembly", "scene:10", "scene:9"], "scene:9"),
        (["voice", "assembly"], "assembly"),
        (["storyboard", "script"], "script"),
        (["scene:1", "storyboard"], "storyboard"),
        (["scene:2", "writer"], "writer"),
    ],
)
def test_the_earliest_node_follows_the_flow_order(nodes: list[str], first: str) -> None:
    assert earliest_node(nodes) == first


@pytest.mark.parametrize(
    ("start", "flagged", "expected"),
    [
        (
            "planner",
            False,
            ["planner", "researcher", "strategist", "writer", "image", "hashtag"],
        ),
        ("strategist", False, ["strategist", "writer", "image", "hashtag"]),
        ("writer", False, ["writer", "hashtag"]),
        ("writer", True, ["writer", "hashtag", "image"]),
        ("image", False, ["image"]),
        ("image", True, ["image"]),
        ("hashtag", False, ["hashtag"]),
    ],
)
def test_the_post_re_entry_table(
    start: str, flagged: bool, expected: list[str]
) -> None:
    assert nodes_to_rerun(start, "post", image_flagged=flagged) == expected


@pytest.mark.parametrize(
    ("start", "expected"),
    [
        (
            "planner",
            [
                "planner",
                "researcher",
                "strategist",
                "script",
                "storyboard",
                "scene:1",
                "scene:2",
                "assembly",
                "voice",
            ],
        ),
        (
            "strategist",
            [
                "strategist",
                "script",
                "storyboard",
                "scene:1",
                "scene:2",
                "assembly",
                "voice",
            ],
        ),
        ("script", ["script", "storyboard", "scene:1", "scene:2", "assembly", "voice"]),
        ("storyboard", ["storyboard", "scene:1", "scene:2", "assembly", "voice"]),
        ("scene:2", ["scene:2", "assembly", "voice"]),
        ("assembly", ["assembly", "voice"]),
        ("voice", ["voice"]),
    ],
)
def test_the_video_re_entry_table(start: str, expected: list[str]) -> None:
    assert nodes_to_rerun(start, "video", scene_keys=["scene:1", "scene:2"]) == expected


# --------------------------------------------------------------------------
# Mechanical platform-fit rules
# --------------------------------------------------------------------------


def run_rules(base: GraphState | None = None, banned: list[str] | None = None) -> Any:
    return rule_issues(base or state(), DEFAULT_PLATFORM_RULES, banned or [])


def test_good_drafts_have_no_rule_issues() -> None:
    assert run_rules() == []


def test_a_missing_or_blank_caption_is_a_writer_error() -> None:
    for drafts in ({}, {"instagram": {"caption": "  "}}, {"instagram": {"caption": 3}}):
        (found,) = run_rules(state(drafts=drafts))
        assert (found.responsible_node, found.platform) == ("writer", "instagram")
        assert found.severity == "error"


def test_caption_and_hashtags_together_must_fit_the_platform_limit() -> None:
    caption = "x" * 2190
    tags = ["#greentea", "#tea", "#morning"]  # 2190 + 2 + 23 = 2215 > 2200
    base = state(drafts={"instagram": draft(caption=caption, hashtags=tags)})

    (found,) = run_rules(base)

    assert found.responsible_node == "writer"
    assert "2215 characters; the limit is 2200" in found.message
    assert "by at least 15 characters" in found.fix_hint


def test_the_total_at_the_limit_passes() -> None:
    tags = ["#greentea", "#tea", "#morning"]
    caption = "x" * (2200 - len("\n\n" + " ".join(tags)))

    assert (
        run_rules(state(drafts={"instagram": draft(caption=caption, hashtags=tags)}))
        == []
    )


def test_description_max_is_the_limit_without_caption_max() -> None:
    rules = {"instagram": {"description_max": 10}}
    base = state(drafts={"instagram": draft(caption="x" * 11, hashtags=[])})

    (found,) = rule_issues(base, rules, [])

    assert "limit is 10" in found.message


@pytest.mark.parametrize(
    ("count", "ok"), [(2, False), (3, True), (15, True), (16, False)]
)
def test_the_hashtag_range_is_a_hashtag_error(count: int, ok: bool) -> None:
    tags = [f"#t{i}" for i in range(count)]
    base = state(drafts={"instagram": draft(hashtags=tags)})

    found = run_rules(base)

    assert (found == []) is ok
    if not ok:
        assert [f.responsible_node for f in found] == ["hashtag"]
        assert "range is 3 to 15" in found[0].message


def test_a_platform_with_only_one_hashtag_bound() -> None:
    base = state(drafts={"instagram": draft(hashtags=["#a"])})

    (low,) = rule_issues(base, {"instagram": {"hashtags_min": 2}}, [])
    (high,) = rule_issues(
        state(drafts={"instagram": draft(hashtags=["#a", "#b", "#c"])}),
        {"instagram": {"hashtags_max": 2}},
        [],
    )

    assert "2 to no maximum" in low.message
    assert "0 to 2" in high.message


def test_missing_hashtags_count_as_zero() -> None:
    base = state(drafts={"instagram": draft(hashtags=None)})

    assert [f.responsible_node for f in run_rules(base)] == ["hashtag"]


def test_non_text_hashtags_are_ignored_in_the_count() -> None:
    base = state(drafts={"instagram": draft(hashtags=[*TAGS, 5, None])})

    assert run_rules(base) == []


@pytest.mark.parametrize("media", [[], None, ["x", {"type": "audio"}]])
def test_a_draft_without_an_image_is_an_image_error(media: Any) -> None:
    base = state(drafts={"instagram": draft(media=media)})

    (found,) = run_rules(base)

    assert found.responsible_node == "image"
    assert "found 0" in found.message


def test_two_images_are_an_image_error() -> None:
    one = draft()["media"][0]
    base = state(drafts={"instagram": draft(media=[one, one])})

    (found,) = run_rules(base)

    assert "found 2" in found.message


def test_a_banned_word_is_a_policy_error_that_does_not_name_it() -> None:
    base = state(drafts={"instagram": draft(caption="A forbidden brew " + "x" * 580)})

    (found,) = run_rules(base, banned=["forbidden"])

    assert found.responsible_node == "writer"
    assert "forbidden" not in found.message + found.fix_hint
    assert "policy" in found.message


def test_a_banned_word_in_a_title_or_hashtag_is_found() -> None:
    title = state(drafts={"instagram": draft(title="Forbidden title")})
    tag = state(drafts={"instagram": draft(hashtags=[*TAGS, "#forbidden"])})

    assert len(run_rules(title, banned=["forbidden"])) == 1
    assert len(run_rules(tag, banned=["forbidden"])) == 1


def test_video_requests_have_no_rule_issues_yet() -> None:
    assert run_rules(video_state(drafts={})) == []


def test_a_platform_without_rules_has_no_bounds() -> None:
    assert rule_issues(state(), {}, []) == []


# --------------------------------------------------------------------------
# reviewer_agent
# --------------------------------------------------------------------------


def test_the_agent_stores_the_review_and_uses_the_text_model(
    fake_llm: FakeScript,
) -> None:
    fake_llm.queue(reply(warnings=["Check the date."]))

    out = make_reviewer_agent()(state())

    assert out.update["review"] == reply(warnings=["Check the date."])
    (call,) = fake_llm.calls
    assert call.kind == "text" and call.temperature == 0.1
    assert out.prompt_version == load_prompt("reviewer").version == "reviewer.v1"
    assert out.summary == {
        "ok": True,
        "errors": 0,
        "rule_errors": 0,
        "scores": scores(),
    }
    assert out.refused_reason is None


def test_the_prompt_gets_the_drafts_without_storage_keys(fake_llm: FakeScript) -> None:
    fake_llm.queue(reply())

    make_reviewer_agent(banned_words=lambda: ["spam"])(
        state(language="ur", brand_voice="playful pirate")
    )

    user = fake_llm.calls[0].messages[1].content
    assert "Content type: post" in user and "Output language: ur" in user
    assert "Brand voice: playful pirate" in user and "Banned words: spam" in user
    assert "Tea is a daily habit" in user and "avoid statistics" in user
    assert '"caption_max": 2200' in user  # the platform's rules
    assert CAPTION in user and "#greentea" in user
    assert "tea on a table" in user and "A cup of tea" in user  # image meta
    assert "storage_key" not in user and "requests/r1" not in user
    assert "provider" not in user


def test_drafts_without_usable_media_are_shown_without_a_media_entry(
    fake_llm: FakeScript,
) -> None:
    fake_llm.queue(reply())
    base = state(drafts={"instagram": draft(media=["junk", None])})

    make_reviewer_agent()(base)

    user = fake_llm.calls[0].messages[1].content
    assert '"media"' not in user and "junk" not in user


def test_missing_inputs_are_sent_as_none(fake_llm: FakeScript) -> None:
    fake_llm.queue(reply())
    base = state(drafts={})
    for key in ("plan", "research", "strategy"):
        del base[key]  # type: ignore[misc]

    make_reviewer_agent(lambda: {})(base)

    user = fake_llm.calls[0].messages[1].content
    assert "Content plan:\nnone" in user and "Drafts per platform:\nnone" in user
    assert 'Platform rules:\n{"instagram": {}}' in user


def test_retry_feedback_reaches_the_prompt(fake_llm: FakeScript) -> None:
    fake_llm.queue(reply())

    make_reviewer_agent()(state(feedback={"reviewer": "Use a real step name."}))

    assert "Use a real step name." in fake_llm.calls[0].messages[1].content


def test_rule_and_llm_issues_are_merged_and_ok_is_recomputed(
    fake_llm: FakeScript,
) -> None:
    base = state(drafts={"instagram": draft(hashtags=["#a"])})  # rule: hashtag range
    fake_llm.queue(reply(), reply(issue("writer", severity="warning")))

    errors = make_reviewer_agent()(base)
    warning_only = make_reviewer_agent()(state())

    review = errors.update["review"]
    assert review["ok"] is False  # the LLM said ok, the rules disagree
    assert [i["responsible_node"] for i in review["issues"]] == ["hashtag"]
    assert errors.summary["rule_errors"] == 1 and errors.summary["errors"] == 1
    assert warning_only.update["review"]["ok"] is True  # warnings do not block


def test_the_rule_issues_come_first(fake_llm: FakeScript) -> None:
    base = state(drafts={"instagram": draft(hashtags=["#a"])})
    fake_llm.queue(reply(issue("writer")))

    review = make_reviewer_agent()(base).update["review"]

    assert [i["responsible_node"] for i in review["issues"]] == ["hashtag", "writer"]


def test_the_llm_ok_flag_is_not_trusted(fake_llm: FakeScript) -> None:
    fake_llm.queue(reply(issue("writer"), ok=True))

    review = make_reviewer_agent()(state()).update["review"]

    assert review["ok"] is False


def test_a_policy_failure_with_a_very_low_policy_score_escalates(
    fake_llm: FakeScript,
) -> None:
    fake_llm.queue(
        reply(issue("planner", message="unsafe topic"), scores=scores(policy=0.1))
    )

    out = make_reviewer_agent()(state())

    assert out.refused_reason == "policy: unsafe topic"
    assert out.update["review"]["ok"] is False


@pytest.mark.parametrize(
    ("policy", "has_errors", "floor", "refused"),
    [
        (0.29, True, 0.3, True),
        (0.3, True, 0.3, False),
        (0.1, False, 0.3, False),  # no error: nothing to escalate
        (0.4, True, 0.5, True),
    ],
)
def test_the_policy_floor(
    fake_llm: FakeScript, policy: float, has_errors: bool, floor: float, refused: bool
) -> None:
    issues = [issue("writer")] if has_errors else []
    fake_llm.queue(reply(*issues, scores=scores(policy=policy)))

    out = make_reviewer_agent(policy_floor=floor)(state())

    assert (out.refused_reason is not None) is refused


def test_the_rules_come_from_the_injected_source(fake_llm: FakeScript) -> None:
    fake_llm.queue(reply())
    custom = {"instagram": {"caption_max": 10}}

    out = make_reviewer_agent(lambda: custom)(state())

    assert out.update["review"]["issues"][0]["message"].endswith("limit is 10.")
    assert '"caption_max": 10' in fake_llm.calls[0].messages[1].content


# --------------------------------------------------------------------------
# Shape checks on the review
# --------------------------------------------------------------------------


def checked(review: Any, base: GraphState | None = None) -> Any:
    return make_check_review()({**(base or state()), "review": review})  # type: ignore[misc]


def test_a_well_formed_review_passes() -> None:
    assert checked(reply(issue("writer"), issue("image", severity="warning"))).ok


def test_no_review_at_all_fails_on_the_scores() -> None:
    assert codes(make_check_review()(state())) == ["scores_invalid"]


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"quality": 0.9},
        {**scores(), "extra": 0.5},
        scores(quality=1.1),
        scores(quality=-0.1),
        scores(quality="high"),
        scores(quality=True),
        scores(quality=None),
        "scores",
        None,
    ],
)
def test_the_scores_need_exactly_the_five_keys_between_zero_and_one(bad: Any) -> None:
    result = checked(reply(scores=bad))

    assert codes(result) == ["scores_invalid"]
    assert "quality, accuracy, policy, platform_fit, brand_voice" in result.feedback


def test_the_boundary_scores_are_allowed() -> None:
    assert checked(reply(scores=scores(quality=0, policy=1))).ok


def test_an_unknown_step_fails_and_lists_the_valid_ones() -> None:
    result = checked(reply(issue("script")))

    assert codes(result) == ["responsible_node_invalid"]
    assert "planner, strategist, writer, image, hashtag" in result.feedback


def test_video_steps_are_valid_for_a_video_and_scenes_are_numbered() -> None:
    base = video_state()
    assert checked(reply(issue("scene:2", platform="instagram")), base).ok is True
    result = checked(reply(issue("writer")), base)

    assert codes(result) == ["responsible_node_invalid"]
    assert "scene:<n>" in result.feedback


def test_a_missing_node_name_fails() -> None:
    data = issue()
    del data["responsible_node"]

    assert codes(checked({**reply(), "issues": [data]})) == ["responsible_node_invalid"]


def test_an_unknown_platform_fails_but_null_is_fine() -> None:
    result = checked(reply(issue(platform="tiktok")))

    assert codes(result) == ["platform_unknown"]
    assert "instagram" in result.feedback
    assert checked(reply(issue(platform=None))).ok is True


@pytest.mark.parametrize("field", ["message", "fix_hint"])
def test_an_error_needs_a_message_and_a_fix_hint(field: str) -> None:
    assert codes(checked(reply(issue(**{field: "  "})))) == ["issue_incomplete"]
    assert checked(reply(issue(severity="warning", **{field: ""}))).ok is True


def test_every_problem_is_reported_in_one_feedback() -> None:
    result = checked(reply(issue("nope", platform="x"), scores={}))

    assert set(codes(result)) == {
        "scores_invalid",
        "responsible_node_invalid",
        "platform_unknown",
    }


def test_the_shape_check_does_not_call_the_llm(fake_llm: FakeScript) -> None:
    checked(reply())

    assert fake_llm.calls == []


# --------------------------------------------------------------------------
# The checked node
# --------------------------------------------------------------------------


class Flow:
    """START -> reviewer (checked) -> END."""

    def __init__(self, **node_args: Any) -> None:
        self.steps: list[StepRecord] = []
        self.node = make_reviewer_node(**node_args)
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


def test_an_ok_review_passes_at_once(fake_llm: FakeScript) -> None:
    fake_llm.queue(reply())
    flow = Flow()

    result = flow.run()

    assert result["review"]["ok"] is True
    (step,) = flow.steps
    assert (step.node_name, step.attempt, step.verdict) == ("reviewer", 1, "passed")
    assert step.output_summary["prompt_version"] == "reviewer.v1"


def test_a_review_with_errors_is_a_valid_result_not_a_failed_attempt(
    fake_llm: FakeScript,
) -> None:
    fake_llm.queue(reply(issue("hashtag")))
    flow = Flow()

    result = flow.run()

    assert result["review"]["ok"] is False
    assert [s.verdict for s in flow.steps] == ["passed"]


def test_a_malformed_review_is_retried_with_feedback(fake_llm: FakeScript) -> None:
    fake_llm.queue(reply(issue("script")), reply(issue("writer")))
    flow = Flow()

    result = flow.run()

    second = fake_llm.calls[1].messages[1].content
    assert "Previous attempt feedback" in second and "is not a step" in second
    assert result["review"]["issues"][0]["responsible_node"] == "writer"
    assert [s.verdict for s in flow.steps] == ["retried", "passed"]


def test_an_unfixable_policy_error_escalates_to_a_human(fake_llm: FakeScript) -> None:
    fake_llm.queue(reply(issue("planner"), scores=scores(policy=0.0)))
    flow = Flow()

    paused = flow.run()

    (interrupt,) = paused["__interrupt__"]
    assert interrupt.value["step"] == "reviewer"
    assert interrupt.value["reason"] == "refused"
    assert interrupt.value["refused"].startswith("policy:")


def test_exhausted_retries_escalate_and_resume_retry_continues(
    fake_llm: FakeScript,
) -> None:
    bad = reply(scores={})
    fake_llm.queue(bad, bad, bad, reply())
    flow = Flow()

    paused = flow.run()

    assert paused["__interrupt__"][0].value["reason"] == "retries_exhausted"
    assert flow.resume({"action": "retry"})["review"]["ok"] is True


def test_the_banned_words_hook_reaches_prompt_and_rules(fake_llm: FakeScript) -> None:
    fake_llm.queue(reply())
    base = state(drafts={"instagram": draft(caption="A forbidden word " + "x" * 580)})

    result = Flow(banned_words=lambda: ["forbidden"]).run(base)

    assert "Banned words: forbidden" in fake_llm.calls[0].messages[1].content
    assert result["review"]["ok"] is False


def test_the_node_uses_the_documented_step_key_and_default_limits() -> None:
    node = make_reviewer_node()

    assert node.key == "reviewer" and node.max_retries == 3
    assert DEFAULT_MAX_REVIEW_CYCLES == 2


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


def reviewed(*issues: dict[str, Any], **changes: Any) -> GraphState:
    return state(review=reply(*issues), **changes)


def test_an_ok_review_clears_rerun_from() -> None:
    update = make_review_gate()(reviewed(rerun_from="writer"))

    assert update == {"rerun_from": None}


def test_warnings_alone_are_ok() -> None:
    assert make_review_gate()(reviewed(issue("writer", severity="warning"))) == {
        "rerun_from": None
    }


def test_no_review_is_ok_for_the_gate() -> None:
    assert make_review_gate()(state()) == {"rerun_from": None}


def test_a_fix_goes_back_to_the_responsible_node_with_its_hint() -> None:
    update = make_review_gate()(
        reviewed(issue("hashtag", fix_hint="Use five tags."), review_cycles=0)
    )

    assert update["rerun_from"] == "hashtag"
    assert update["review_cycles"] == 1
    assert update["feedback"] == {"hashtag": "Use five tags."}
    assert update["retries"] == {"hashtag": 0}
    assert update["status"] == "generating"


def test_the_earliest_responsible_node_is_where_the_flow_restarts() -> None:
    update = make_review_gate()(
        reviewed(issue("hashtag"), issue("writer", fix_hint="Shorten."), issue("image"))
    )

    assert update["rerun_from"] == "writer"
    assert update["feedback"] == {
        "hashtag": "fix the hashtag",
        "writer": "Shorten.",
        "image": "fix the image",
    }
    # An image the reviewer flagged reruns together with the writer.
    assert set(update["retries"]) == {"writer", "hashtag", "image"}


def test_a_writer_fix_does_not_reset_the_image_counter() -> None:
    update = make_review_gate()(
        reviewed(issue("writer"), retries={"image": 2, "writer": 3, "hashtag": 1})
    )

    assert update["retries"] == {"image": 2, "writer": 0, "hashtag": 0}


def test_several_hints_for_one_node_are_joined_and_clipped() -> None:
    long = "x" * 900
    update = make_review_gate()(
        reviewed(issue("writer", fix_hint="First."), issue("writer", fix_hint=long))
    )

    feedback = update["feedback"]["writer"]
    assert feedback.startswith("First. xxx") and len(feedback) == 500
    assert feedback.endswith("…")


def test_existing_feedback_of_other_steps_is_kept() -> None:
    update = make_review_gate()(
        reviewed(issue("hashtag"), feedback={"planner": None, "writer": "old"})
    )

    assert update["feedback"] == {
        "planner": None,
        "writer": "old",
        "hashtag": "fix the hashtag",
    }


def test_the_cycle_counter_keeps_counting() -> None:
    update = make_review_gate()(reviewed(issue("writer"), review_cycles=1))

    assert update["review_cycles"] == 2


def test_at_the_cap_the_errors_become_warnings_and_the_run_continues() -> None:
    base = reviewed(
        issue("writer", message="Weak hook."),
        issue("image", platform=None, message="Off topic."),
        review_cycles=2,
        rerun_from="writer",
    )
    base["review"]["warnings"] = ["Check the date."]

    update = make_review_gate()(base)

    assert update["rerun_from"] is None and "review_cycles" not in update
    assert update["review"]["warnings"] == [
        "Check the date.",
        "instagram: Weak hook.",
        "all: Off topic.",
    ]
    assert update["review"]["issues"] == base["review"]["issues"]
    assert "feedback" not in update and "retries" not in update


def test_the_cap_is_a_parameter() -> None:
    gate = make_review_gate(max_review_cycles=1)

    assert gate(reviewed(issue("writer"), review_cycles=0))["rerun_from"] == "writer"
    assert gate(reviewed(issue("writer"), review_cycles=1))["rerun_from"] is None


def test_a_cap_of_zero_never_loops() -> None:
    update = make_review_gate(max_review_cycles=0)(reviewed(issue("writer")))

    assert update["rerun_from"] is None and update["review"]["warnings"]


def test_a_negative_cap_is_refused() -> None:
    with pytest.raises(ValueError, match="max_review_cycles"):
        make_review_gate(max_review_cycles=-1)


def test_long_warnings_are_clipped() -> None:
    base = reviewed(issue("writer", message="m" * 600), review_cycles=2)

    (warning,) = make_review_gate()(base)["review"]["warnings"]

    assert len(warning) == 300 and warning.endswith("…")


def test_video_scenes_restart_at_the_earliest_scene_and_reset_the_later_steps() -> None:
    base = video_state(
        review=reply(issue("scene:3"), issue("scene:2"), issue("voice")),
        retries={"scene:1": 1, "scene:2": 2, "assembly": 1, "script": 1},
    )

    update = make_review_gate()(base)

    assert update["rerun_from"] == "scene:2"
    assert update["retries"] == {
        "scene:1": 1,  # earlier: untouched
        "scene:2": 0,
        "scene:3": 0,
        "assembly": 0,
        "script": 1,
        "voice": 0,
    }


def test_the_gate_does_not_mutate_its_input() -> None:
    base = reviewed(issue("writer"), feedback={"writer": "old"}, retries={"writer": 2})

    make_review_gate()(base)

    assert base["feedback"] == {"writer": "old"} and base["retries"] == {"writer": 2}
    assert "review_cycles" not in base


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rerun_from", "route"),
    [
        (None, "ok"),
        ("", "ok"),
        ("planner", "planner"),
        ("strategist", "strategist"),
        ("writer", "content"),
        ("image", "content"),
        ("hashtag", "content"),
        ("script", "content"),
        ("scene:2", "content"),
        ("voice", "content"),
    ],
)
def test_route_after_review(rerun_from: Any, route: str) -> None:
    assert route_after_review(state(rerun_from=rerun_from)) == route


def test_a_missing_rerun_from_means_ok() -> None:
    assert route_after_review(state()) == "ok"


@pytest.mark.parametrize(
    ("status", "route"),
    [("failed", "failed"), ("generating", "review"), (None, "review")],
)
def test_the_review_is_skipped_after_a_failed_content_step(
    status: Any, route: str
) -> None:
    base = state()
    if status is not None:
        base["status"] = status

    assert route_after_content(base) == route
