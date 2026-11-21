"""``hashtag``: the hashtags per platform (AGENT_SPECS 4.3).

They are chosen from the final caption, so this step runs after the writer. It
is rule-only (no judge): every rule in the spec is mechanical. The tags go to
``drafts[platform]["hashtags"]``; other draft fields are kept.
"""

import re
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.agents.checked import (
    DEFAULT_MAX_RETRIES,
    AgentOutput,
    CheckedNode,
    CheckResult,
    Issue,
    make_checked_node,
)
from app.agents.llm import generate_structured
from app.agents.nodes.validate_input import (
    BannedWords,
    contains_banned_word,
    no_banned_words,
)
from app.agents.platform_rules import PlatformRules, default_platform_rules
from app.agents.post.common import (
    error,
    is_number,
    platform_rule,
    require_strategy,
    target_platforms,
    to_json,
)
from app.agents.prompts import load_prompt
from app.agents.state import GraphState

STEP_KEY = "hashtag"
HASHTAG_TEMPERATURE = 0.5  # AGENT_SPECS 1.4
MAX_TAG_CHARS = 40  # including the "#"
_TAG = re.compile(r"#\w+")
_MAX_LISTED = 5  # tags named in one message


class HashtagOutput(BaseModel):
    """The hashtag agent's reply (``AGENT_SPECS.md`` 4.3)."""

    model_config = ConfigDict(str_strip_whitespace=True)

    hashtags: list[str]


def make_hashtag_agent(
    platform_rules: Callable[[], PlatformRules] = default_platform_rules,
    banned_words: BannedWords = no_banned_words,
) -> Callable[[GraphState], AgentOutput]:
    """Build the agent function; the retry feedback is added by the prompt loader."""

    def hashtag_agent(state: GraphState) -> AgentOutput:
        template = load_prompt("hashtag")
        rules = platform_rules()
        banned = ", ".join(banned_words())
        feedback = (state.get("feedback") or {}).get(STEP_KEY)
        drafts = dict(state.get("drafts") or {})
        platforms = target_platforms(state)

        for platform in platforms:
            caption = (drafts.get(platform) or {}).get("caption")
            if not caption:
                raise ValueError(
                    f"the hashtag step needs the {platform} caption; run the writer"
                )
            rule = platform_rule(rules, platform)
            messages = template.messages(
                feedback=feedback,
                platform=platform,
                hashtag_count=require_strategy(state, platform).get("hashtag_count"),
                hashtag_min=rule.get("hashtags_min"),
                hashtag_max=rule.get("hashtags_max"),
                banned_words=banned,
                language=state["language"],
                caption=caption,
                plan=to_json(state.get("plan")),
            )
            reply = generate_structured(
                HashtagOutput, messages, temperature=HASHTAG_TEMPERATURE
            )
            draft = dict(drafts[platform])
            draft["hashtags"] = list(reply.hashtags)
            drafts[platform] = draft  # type: ignore[assignment]

        return AgentOutput(
            update={"drafts": drafts},
            prompt_version=template.version,
            summary={
                "platforms": platforms,
                "hashtag_counts": {p: len(drafts[p]["hashtags"]) for p in platforms},
            },
        )

    return hashtag_agent


def _names(tags: list[str]) -> str:
    shown = ", ".join(tags[:_MAX_LISTED])
    return shown + ", ..." if len(tags) > _MAX_LISTED else shown


def _tag_issues(platform: str, tags: list[Any]) -> list[Issue]:
    issues: list[Issue] = []
    malformed = [t for t in tags if not isinstance(t, str) or not _TAG.fullmatch(t)]
    if malformed:
        issues.append(
            error(
                "hashtag_invalid",
                f"{platform}: each hashtag must start with # and use only letters, "
                f"digits or underscores, without spaces. Fix: "
                f"{_names([str(t) for t in malformed])}.",
            )
        )
    too_long = [t for t in tags if isinstance(t, str) and len(t) > MAX_TAG_CHARS]
    if too_long:
        issues.append(
            error(
                "hashtag_too_long",
                f"{platform}: hashtags can have at most {MAX_TAG_CHARS} characters. "
                f"Shorten: {_names(too_long)}.",
            )
        )
    seen: set[str] = set()
    repeated: list[str] = []
    for tag in tags:
        folded = str(tag).casefold()
        if folded in seen:
            repeated.append(str(tag))
        seen.add(folded)
    if repeated:
        issues.append(
            error(
                "hashtag_duplicate",
                f"{platform}: remove the repeated hashtags ({_names(repeated)}); "
                "ignore upper and lower case.",
            )
        )
    return issues


def _range_issue(platform: str, count: int, rule: Mapping[str, Any]) -> list[Issue]:
    low, high = rule.get("hashtags_min"), rule.get("hashtags_max")
    if (is_number(low) and count < low) or (is_number(high) and count > high):
        return [
            error(
                "hashtag_count_range",
                f"{platform}: give between {low if is_number(low) else 0} and "
                f"{high if is_number(high) else 'the platform maximum'} hashtags, "
                f"not {count}.",
            )
        ]
    return []


def make_check_hashtag(
    platform_rules: Callable[[], PlatformRules] = default_platform_rules,
    banned_words: BannedWords = no_banned_words,
) -> Callable[[GraphState], CheckResult]:
    """Rule checks (AGENT_SPECS 4.3): a list of well-formed tags (``#`` then letters,
    digits or ``_``), at most 40 characters each, unique ignoring case, within the
    platform's min and max, none banned. The broad/niche mix and topic fit are
    prompt rules, not mechanical checks. Rules are read on every run.
    """

    def check(state: GraphState) -> CheckResult:
        drafts = state.get("drafts") or {}
        rules = platform_rules()
        banned = banned_words()
        issues: list[Issue] = []

        for platform in target_platforms(state):
            tags = (drafts.get(platform) or {}).get("hashtags")
            if not isinstance(tags, list):
                issues.append(
                    error("hashtags_missing", f"{platform}: add the hashtags.")
                )
                continue
            issues += _tag_issues(platform, tags)
            issues += _range_issue(platform, len(tags), platform_rule(rules, platform))
            if contains_banned_word([str(t).lstrip("#") for t in tags], banned):
                issues.append(
                    error(
                        "banned_word",
                        f"{platform}: a hashtag is a banned word. Replace it.",
                    )
                )
        return CheckResult.from_issues(issues)

    return check


def make_hashtag_node(
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    platform_rules: Callable[[], PlatformRules] = default_platform_rules,
    banned_words: BannedWords = no_banned_words,
) -> CheckedNode:
    """The hashtag step as a checked node (step key ``hashtag``), rule checks only."""
    return make_checked_node(
        STEP_KEY,
        make_hashtag_agent(platform_rules, banned_words),
        make_check_hashtag(platform_rules, banned_words),
        max_retries,
    )
