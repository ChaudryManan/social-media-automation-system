"""Tests for ``validate_input`` and the platform-rule helpers (B2-05)."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from app.agents.nodes import validate_input as vi
from app.agents.nodes.validate_input import (
    collect_errors,
    contains_banned_word,
    make_validate_input_node,
    no_banned_words,
    route_after_validation,
)
from app.agents.platform_rules import (
    DEFAULT_PLATFORM_RULES,
    default_platform_rules,
    platform_supports,
    supported_content_types,
)
from app.agents.state import GraphState
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph


def good(**changes: Any) -> GraphState:
    state: dict[str, Any] = {
        "request_id": "r1",
        "workspace_id": "w1",
        "user_id": "u1",
        "topic": "Benefits of green tea",
        "content_type": "post",
        "target_accounts": [
            {"account_id": "acc-1", "platform": "facebook"},
            {"account_id": "acc-2", "platform": "instagram"},
        ],
        "publish_at": "2026-12-01T10:00:00+00:00",
        "brand_voice": "warm and simple",
        "language": "en",
        "options": {"tone": "playful"},
    }
    state.update(changes)
    return state  # type: ignore[return-value]


node = make_validate_input_node()


# --------------------------------------------------------------------------
# Happy path and the node contract
# --------------------------------------------------------------------------


def test_a_valid_request_passes() -> None:
    assert collect_errors(good()) == []
    assert node(good()) == {"status": "generating"}


def test_a_valid_request_with_optional_fields_missing_passes() -> None:
    state = good()
    del state["brand_voice"]
    del state["options"]

    assert collect_errors(state) == []


def test_an_invalid_request_fails_with_all_problems_at_once() -> None:
    result = node(good(topic="!!", content_type="gif", language=""))

    assert result["status"] == "failed"
    assert result["errors"] == [
        "Topic must be at least 3 characters.",
        "Topic must contain letters or numbers, not only symbols.",
        "Content type must be one of: post, video.",
        "Language is required.",
    ]


def test_errors_are_appended_to_existing_ones() -> None:
    result = node(good(topic="", errors=["earlier problem"]))

    assert result["errors"] == ["earlier problem", "Topic is required."]


def test_the_node_does_not_modify_its_input() -> None:
    state = good(topic="x")
    snapshot = copy.deepcopy(state)

    node(state)

    assert state == snapshot


@pytest.mark.parametrize(
    ("status", "expected"),
    [("failed", "failed"), ("generating", "ok"), (None, "ok")],
)
def test_route_after_validation(status: str | None, expected: str) -> None:
    state: dict[str, Any] = {} if status is None else {"status": status}

    assert route_after_validation(state) == expected  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Topic
# --------------------------------------------------------------------------


@pytest.mark.parametrize("topic", [None, "", "   ", "\n\t"])
def test_missing_or_blank_topic(topic: Any) -> None:
    assert collect_errors(good(topic=topic)) == ["Topic is required."]


@pytest.mark.parametrize("topic", [123, ["tea"], {"a": 1}, True])
def test_non_text_topic(topic: Any) -> None:
    assert collect_errors(good(topic=topic)) == ["Topic must be text."]


@pytest.mark.parametrize(
    ("topic", "valid"),
    [
        ("ab", False),
        ("abc", True),
        ("  ab  ", False),  # length is measured after trimming
        ("  abc  ", True),
        ("a" * 500, True),
        ("a" * 501, False),
        (" " + "a" * 500 + " ", True),
        ("日本語", True),  # characters, not bytes
    ],
)
def test_topic_length_limits(topic: str, valid: bool) -> None:
    assert (collect_errors(good(topic=topic)) == []) is valid


@pytest.mark.parametrize("topic", ["!!!", "…?!", "😀😀😀", "---", "@#$%"])
def test_topic_with_only_symbols(topic: str) -> None:
    assert collect_errors(good(topic=topic)) == [
        "Topic must contain letters or numbers, not only symbols."
    ]


@pytest.mark.parametrize("topic", ["2026", "C++", "#tea", "Café ☕"])
def test_topics_with_letters_or_digits_are_fine(topic: str) -> None:
    assert collect_errors(good(topic=topic)) == []


# --------------------------------------------------------------------------
# Content type
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "gif", "POST", 1])
def test_unknown_content_type(value: Any) -> None:
    errors = collect_errors(good(content_type=value))

    assert errors == ["Content type must be one of: post, video."]


@pytest.mark.parametrize("value", ["post", "video"])
def test_known_content_types(value: str) -> None:
    accounts = [{"account_id": "a", "platform": "instagram"}]

    assert collect_errors(good(content_type=value, target_accounts=accounts)) == []


# --------------------------------------------------------------------------
# Target accounts
# --------------------------------------------------------------------------


@pytest.mark.parametrize("accounts", [None, [], (), "acc", {"account_id": "a"}, 5])
def test_target_accounts_are_required(accounts: Any) -> None:
    assert collect_errors(good(target_accounts=accounts)) == [
        "At least one target account is required."
    ]


@pytest.mark.parametrize(
    "account",
    [
        None,
        "acc-1",
        {},
        {"platform": "facebook"},
        {"account_id": "", "platform": "facebook"},
        {"account_id": "  ", "platform": "facebook"},
        {"account_id": 7, "platform": "facebook"},
    ],
)
def test_account_without_an_id(account: Any) -> None:
    errors = collect_errors(good(target_accounts=[account]))

    assert errors == ["Target account 1 has no account id."]


@pytest.mark.parametrize("platform", [None, "", "myspace", "Facebook", 3])
def test_account_with_an_unsupported_platform(platform: Any) -> None:
    accounts = [{"account_id": "a", "platform": platform}]

    errors = collect_errors(good(target_accounts=accounts))

    assert errors == ["Target account 1 has an unsupported platform."]


def test_account_problems_name_the_position() -> None:
    accounts = [
        {"account_id": "a", "platform": "facebook"},
        {"account_id": "", "platform": "facebook"},
        {"account_id": "c", "platform": "nope"},
    ]

    errors = collect_errors(good(target_accounts=accounts))

    assert errors == [
        "Target account 2 has no account id.",
        "Target account 3 has an unsupported platform.",
    ]


def test_the_same_account_twice_is_reported_once() -> None:
    accounts = [
        {"account_id": "a", "platform": "facebook"},
        {"account_id": "a", "platform": "facebook"},
        {"account_id": "a", "platform": "facebook"},
    ]

    assert collect_errors(good(target_accounts=accounts)) == [
        "The same account is selected more than once."
    ]


def test_two_accounts_on_the_same_platform_are_allowed() -> None:
    accounts = [
        {"account_id": "a", "platform": "facebook"},
        {"account_id": "b", "platform": "facebook"},
    ]

    assert collect_errors(good(target_accounts=accounts)) == []


# --------------------------------------------------------------------------
# Content type vs platform
# --------------------------------------------------------------------------


def test_a_post_for_a_video_only_platform_is_an_error() -> None:
    accounts = [
        {"account_id": "a", "platform": "facebook"},
        {"account_id": "b", "platform": "tiktok"},
    ]

    errors = collect_errors(good(content_type="post", target_accounts=accounts))

    assert errors == ["tiktok does not support post content."]


def test_each_unsupported_platform_is_reported_once() -> None:
    accounts = [
        {"account_id": "a", "platform": "tiktok"},
        {"account_id": "b", "platform": "tiktok"},
        {"account_id": "c", "platform": "youtube"},
    ]

    errors = collect_errors(good(content_type="post", target_accounts=accounts))

    assert errors == [
        "tiktok does not support post content.",
        "youtube does not support post content.",
    ]


def test_video_is_supported_by_every_default_platform() -> None:
    accounts = [
        {"account_id": str(i), "platform": p}
        for i, p in enumerate(("facebook", "instagram", "tiktok", "youtube"))
    ]

    assert collect_errors(good(content_type="video", target_accounts=accounts)) == []


def test_platform_rules_come_from_the_injected_source() -> None:
    rules = {"facebook": {"allowed_formats": ["post"]}, "instagram": {}}
    custom = make_validate_input_node(platform_rules=lambda: rules)
    accounts = [{"account_id": "a", "platform": "facebook"}]

    assert custom(good(content_type="post", target_accounts=accounts)) == {
        "status": "generating"
    }
    failed = custom(good(content_type="video", target_accounts=accounts))
    assert failed["errors"] == ["facebook does not support video content."]
    failed = custom(
        good(
            content_type="post",
            target_accounts=[{"account_id": "i", "platform": "instagram"}],
        )
    )
    assert failed["errors"] == ["instagram does not support post content."]


def test_platform_missing_from_the_rules_is_unsupported() -> None:
    custom = make_validate_input_node(platform_rules=lambda: {})

    result = custom(good())

    assert result["status"] == "failed"
    assert len(result["errors"]) == 2


def test_platform_rules_are_read_on_every_run() -> None:
    rules: dict[str, Any] = {"facebook": {"allowed_formats": ["post"]}}
    custom = make_validate_input_node(platform_rules=lambda: rules)
    accounts = [{"account_id": "a", "platform": "facebook"}]
    assert custom(good(target_accounts=accounts))["status"] == "generating"

    rules["facebook"] = {"allowed_formats": ["video"]}

    assert custom(good(target_accounts=accounts))["status"] == "failed"


# --------------------------------------------------------------------------
# Language, publish_at, brand voice, options
# --------------------------------------------------------------------------


@pytest.mark.parametrize("language", [None, "", "   "])
def test_language_is_required(language: Any) -> None:
    assert collect_errors(good(language=language)) == ["Language is required."]


@pytest.mark.parametrize(
    "language", ["e", "english-", "en_US", "123", "en us", 5, "x" * 40]
)
def test_malformed_language_tags(language: Any) -> None:
    assert collect_errors(good(language=language)) == [
        "Language must be a language tag such as 'en' or 'pt-BR'."
    ]


@pytest.mark.parametrize("language", ["en", "ur", "pt-BR", "zh-Hant-TW", "fil", " en "])
def test_valid_language_tags(language: str) -> None:
    assert collect_errors(good(language=language)) == []


@pytest.mark.parametrize("value", [None, "", "  "])
def test_publish_at_is_required(value: Any) -> None:
    assert collect_errors(good(publish_at=value)) == ["Publish time is required."]


@pytest.mark.parametrize(
    "value",
    [
        "tomorrow",
        "2026-13-01T10:00:00+00:00",
        "2026-12-01",  # date only
        "2026-12-01T10:00:00",  # no time zone
        "10:00",
        12345,
        ["2026-12-01T10:00:00Z"],
    ],
)
def test_invalid_publish_at(value: Any) -> None:
    errors = collect_errors(good(publish_at=value))

    assert errors == [
        "Publish time must be an ISO-8601 date and time with a time zone."
    ]


@pytest.mark.parametrize(
    "value",
    [
        "2026-12-01T10:00:00Z",
        "2026-12-01T10:00:00+00:00",
        "2026-12-01T15:00:00+05:00",
        " 2026-12-01T10:00:00Z ",
    ],
)
def test_valid_publish_at(value: str) -> None:
    assert collect_errors(good(publish_at=value)) == []


def test_publish_at_in_the_past_is_not_this_nodes_business() -> None:
    # "publish_at is in the future" is checked at submission (B3-05)
    assert collect_errors(good(publish_at="2001-01-01T00:00:00Z")) == []


def test_brand_voice_must_be_text_when_given() -> None:
    assert collect_errors(good(brand_voice=None)) == []
    assert collect_errors(good(brand_voice=5)) == ["Brand voice must be text."]


@pytest.mark.parametrize("options", ["tone", ["a"], 5])
def test_options_must_be_an_object(options: Any) -> None:
    assert collect_errors(good(options=options)) == ["Options must be an object."]


def test_options_must_be_plain_json_values() -> None:
    assert collect_errors(good(options={"x": object()})) == [
        "Options must contain only plain values."
    ]


def test_options_size_limit() -> None:
    fits = {"k": "a" * (vi.MAX_OPTIONS_CHARS - 10)}
    too_big = {"k": "a" * vi.MAX_OPTIONS_CHARS}

    assert collect_errors(good(options=fits)) == []
    assert collect_errors(good(options=too_big)) == [
        f"Options must be at most {vi.MAX_OPTIONS_CHARS} characters."
    ]


# --------------------------------------------------------------------------
# Moderation (banned words)
# --------------------------------------------------------------------------

BANNED_ERROR = "The request contains a word that is not allowed."


def banned_node(*words: str) -> Any:
    return make_validate_input_node(banned_words=lambda: list(words))


def test_the_default_moderation_stub_blocks_nothing() -> None:
    assert list(no_banned_words()) == []
    assert node(good(topic="anything goes here"))["status"] == "generating"


def test_banned_word_in_the_topic_fails() -> None:
    result = banned_node("scam")(good(topic="How to run a SCAM online"))

    assert result == {"status": "failed", "errors": [BANNED_ERROR]}


def test_the_message_never_names_the_matched_word() -> None:
    result = banned_node("secretword")(good(topic="about secretword today"))

    assert "secretword" not in " ".join(result["errors"])


@pytest.mark.parametrize(
    ("topic", "blocked"),
    [
        ("class notes", False),  # not inside another word
        ("an ass in a field", True),
        ("Ass.", True),  # punctuation is a boundary
        ("ASS-kicking", True),
        ("passing", False),
        ("assassin", False),
    ],
)
def test_banned_words_match_whole_words_only(topic: str, blocked: bool) -> None:
    result = banned_node("ass")(good(topic=topic))

    assert (result["status"] == "failed") is blocked


def test_banned_phrases_match_as_a_whole() -> None:
    node_ = banned_node("get rich quick")

    assert (
        node_(good(topic="Get  rich quick tips"))["status"] == "generating"
    )  # two spaces
    assert node_(good(topic="get rich quick tips"))["status"] == "failed"
    assert node_(good(topic="get rich, quick"))["status"] == "generating"


def test_banned_words_are_matched_case_insensitively_with_unicode() -> None:
    assert banned_node("ÉCOLE")(good(topic="une école"))["status"] == "failed"
    assert banned_node("straße")(good(topic="STRASSE und straße"))["status"] == "failed"


def test_regex_characters_in_a_banned_word_are_literal() -> None:
    assert banned_node("c++")(good(topic="learn c++ today"))["status"] == "failed"
    assert banned_node(".*")(good(topic="learn python today"))["status"] == "generating"
    assert banned_node("(")(good(topic="learn python today"))["status"] == "generating"


@pytest.mark.parametrize("word", ["", "   ", None, 5])
def test_empty_or_invalid_banned_entries_are_ignored(word: Any) -> None:
    assert banned_node(word)(good())["status"] == "generating"  # type: ignore[arg-type]


def test_banned_words_are_checked_in_brand_voice_and_options() -> None:
    node_ = banned_node("forbidden")

    assert node_(good(brand_voice="forbidden tone"))["status"] == "failed"
    assert node_(good(options={"note": "so forbidden"}))["status"] == "failed"
    assert node_(good(options={"a": {"b": ["x", "forbidden"]}}))["status"] == "failed"


def test_banned_words_deeper_than_the_limit_are_not_scanned() -> None:
    nested: Any = "forbidden"
    for _ in range(vi._MAX_TEXT_DEPTH + 2):
        nested = {"k": nested}

    assert banned_node("forbidden")(good(options=nested))["status"] == "generating"


def test_banned_words_are_not_matched_in_non_text_fields() -> None:
    result = banned_node("facebook")(good())  # platform names are not user text

    assert result["status"] == "generating"


def test_contains_banned_word_edge_cases() -> None:
    assert contains_banned_word([], ["x"]) is False
    assert contains_banned_word(["x"], []) is False
    assert contains_banned_word(["a x b"], ["x"]) is True
    assert contains_banned_word(["axb"], ["x"]) is False


def test_banned_words_are_read_on_every_run() -> None:
    words: list[str] = []
    node_ = make_validate_input_node(banned_words=lambda: words)
    assert node_(good(topic="tea scam"))["status"] == "generating"

    words.append("scam")

    assert node_(good(topic="tea scam"))["status"] == "failed"


# --------------------------------------------------------------------------
# Account and quota hooks (filled in by B3)
# --------------------------------------------------------------------------


def test_account_hook_messages_become_errors() -> None:
    custom = make_validate_input_node(
        check_accounts=lambda s: [
            "Account acc-2 is disconnected.",
            "Account acc-9 is unknown.",
        ]
    )

    result = custom(good())

    assert result == {
        "status": "failed",
        "errors": ["Account acc-2 is disconnected.", "Account acc-9 is unknown."],
    }


def test_quota_hook_message_becomes_an_error() -> None:
    custom = make_validate_input_node(check_quota=lambda s: "Monthly limit reached.")

    assert custom(good())["errors"] == ["Monthly limit reached."]


@pytest.mark.parametrize("message", [None, ""])
def test_quota_hook_without_a_message_passes(message: str | None) -> None:
    custom = make_validate_input_node(check_quota=lambda s: message)

    assert custom(good()) == {"status": "generating"}


def test_hooks_receive_the_state_and_run_only_for_a_well_formed_request() -> None:
    seen: list[GraphState] = []
    custom = make_validate_input_node(
        check_accounts=lambda s: seen.append(s) or [],
        check_quota=lambda s: seen.append(s) or None,
    )

    custom(good(topic=""))
    assert seen == []

    state = good()
    custom(state)
    assert seen == [state, state]


def test_hook_messages_are_cleaned_and_clipped() -> None:
    custom = make_validate_input_node(
        check_accounts=lambda s: ["  two\n lines   here ", "x" * 500]
    )

    errors = custom(good())["errors"]

    assert errors[0] == "two lines here"
    assert len(errors[1]) == 200
    assert errors[1].endswith("…")


def test_duplicate_hook_messages_are_reported_once() -> None:
    custom = make_validate_input_node(
        check_accounts=lambda s: ["Same."], check_quota=lambda s: "Same."
    )

    assert custom(good())["errors"] == ["Same."]


def test_both_hooks_report_together() -> None:
    custom = make_validate_input_node(
        check_accounts=lambda s: ["Account problem."],
        check_quota=lambda s: "Quota problem.",
    )

    assert custom(good())["errors"] == ["Account problem.", "Quota problem."]


# --------------------------------------------------------------------------
# As a graph node: invalid input ends the graph before anything else runs
# --------------------------------------------------------------------------


def build_graph(validate: Any) -> tuple[Any, list[str]]:
    ran: list[str] = []
    builder = StateGraph(GraphState)
    builder.add_node("validate_input", validate)
    builder.add_node("planner", lambda s: ran.append("planner") or {"plan": {"x": 1}})
    builder.add_edge(START, "validate_input")
    builder.add_conditional_edges(
        "validate_input", route_after_validation, {"ok": "planner", "failed": END}
    )
    builder.add_edge("planner", END)
    return builder.compile(checkpointer=InMemorySaver()), ran


def run(graph: Any, state: GraphState) -> dict[str, Any]:
    return graph.invoke(state, {"configurable": {"thread_id": "t"}})


def test_valid_input_continues_to_the_next_node() -> None:
    graph, ran = build_graph(node)

    result = run(graph, good())

    assert ran == ["planner"]
    assert result["status"] == "generating"
    assert result["plan"] == {"x": 1}
    assert "errors" not in result or result["errors"] == []


def test_invalid_input_ends_the_graph_with_errors() -> None:
    graph, ran = build_graph(node)

    result = run(graph, good(topic="", language=""))

    assert ran == []  # the planner never ran
    assert result["status"] == "failed"
    assert result["errors"] == ["Topic is required.", "Language is required."]
    assert "plan" not in result


# --------------------------------------------------------------------------
# platform_rules helpers
# --------------------------------------------------------------------------


def test_default_rules_equal_the_json_in_agent_specs_section_2() -> None:
    text = (Path(__file__).resolve().parents[2] / "AGENT_SPECS.md").read_text(
        encoding="utf-8"
    )
    block = text.split("## 2. Platform rules")[1].split("```json")[1].split("```")[0]

    assert DEFAULT_PLATFORM_RULES == json.loads(block)


def test_default_rules_match_agent_specs_section_2() -> None:
    assert set(DEFAULT_PLATFORM_RULES) == {"facebook", "instagram", "tiktok", "youtube"}
    assert default_platform_rules() is DEFAULT_PLATFORM_RULES
    assert DEFAULT_PLATFORM_RULES["instagram"]["caption_max"] == 2200
    assert DEFAULT_PLATFORM_RULES["instagram"]["hashtags_min"] == 3
    assert DEFAULT_PLATFORM_RULES["tiktok"]["video_max_seconds"] == 180
    assert DEFAULT_PLATFORM_RULES["youtube"]["title_max"] == 100


@pytest.mark.parametrize(
    ("platform", "types"),
    [
        ("facebook", {"post", "video"}),
        ("instagram", {"post", "video"}),
        ("tiktok", {"video"}),
        ("youtube", {"video"}),
    ],
)
def test_content_types_derived_from_the_default_rules(
    platform: str, types: set[str]
) -> None:
    assert supported_content_types(DEFAULT_PLATFORM_RULES[platform]) == types


def test_explicit_allowed_formats_win_over_aspect_ratios() -> None:
    rule = {
        "allowed_formats": ["post"],
        "video_aspect": ["9:16"],
        "image_aspect": ["1:1"],
    }

    assert supported_content_types(rule) == {"post"}


def test_unknown_formats_in_allowed_formats_are_dropped() -> None:
    assert supported_content_types({"allowed_formats": ["post", "story", 3]}) == {
        "post"
    }
    assert supported_content_types({"allowed_formats": []}) == frozenset()


def test_empty_rules_support_nothing() -> None:
    assert supported_content_types({}) == frozenset()
    assert (
        supported_content_types({"image_aspect": [], "video_aspect": None})
        == frozenset()
    )


@pytest.mark.parametrize(
    ("platform", "content_type", "expected"),
    [
        ("facebook", "post", True),
        ("tiktok", "post", False),
        ("tiktok", "video", True),
        ("myspace", "post", False),
        ("facebook", "gif", False),
    ],
)
def test_platform_supports(platform: str, content_type: str, expected: bool) -> None:
    assert platform_supports(DEFAULT_PLATFORM_RULES, platform, content_type) is expected


def test_platform_supports_ignores_a_malformed_rule() -> None:
    assert platform_supports({"facebook": None}, "facebook", "post") is False  # type: ignore[dict-item]
