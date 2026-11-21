"""``writer``: the caption (and the YouTube title) per platform (AGENT_SPECS 4.1).

One checked step writes every distinct target platform (FR-5.5: generated once
per platform and reused for all its accounts). The result goes to
``drafts[platform]["caption"]`` (and ``["title"]``); other draft fields
(hashtags, media) are kept, so a reviewer re-entry at the writer does not wipe
them. ``hook``, ``cta`` and ``language`` are part of the model's reply but are
not draft fields (``ARCHITECTURE.md`` 6.2), so the rule checks derive what they
need from the caption and the judge checks the language and the hook.
"""

import math
import re
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.agents.checked import (
    DEFAULT_JUDGE_PASS_THRESHOLD,
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

STEP_KEY = "writer"
WRITER_TEMPERATURE = 0.8  # AGENT_SPECS 1.4: creative
LENGTH_TOLERANCE = 0.3  # caption length within +-30% of the strategy's target
MAX_REFUSAL_CHARS = 200
DEFAULT_REFUSAL = "the topic cannot be written safely"

WRITER_RUBRIC = """\
Score from 0 to 1 on these five points, equally weighted:
1. Hook strength: the first line of every caption makes people want to read on.
2. Clarity: the text is easy to follow and has no filler.
3. Tone and language: it follows the brand voice and the strategy, reads natively
   for its platform and is written in the requested output language (not mixed).
4. Factual safety: every specific claim, number or source is supported by the
   research notes below; general statements are fine.
5. Call to action: it fits the strategy's call to action, if there is one.
Report an error issue for invented facts, statistics or quotes, misleading or
sensational claims, text in the wrong language, and a caption that ignores the plan."""

_HASHTAG_IN_TEXT = re.compile(r"(?<![\w&/])#[^\W\d_]")
_URL = re.compile(r"https?://\S+|\bwww\.\S+", re.IGNORECASE)
_PLACEHOLDER = re.compile(r"\{\{|\}\}|\{[A-Za-z_]\w*\}|\[[^\]\n]{1,40}\]")


class WriterOutput(BaseModel):
    """The writer's reply for one platform (``AGENT_SPECS.md`` 4.1)."""

    model_config = ConfigDict(str_strip_whitespace=True)

    caption: str  # body text WITHOUT hashtags
    title: str | None = None  # required for youtube, else None
    hook: str  # first line (also appears in caption)
    cta: str | None = None
    language: str
    refused: bool = False
    reason: str | None = None


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def make_writer_agent(
    platform_rules: Callable[[], PlatformRules] = default_platform_rules,
    banned_words: BannedWords = no_banned_words,
) -> Callable[[GraphState], AgentOutput]:
    """Build the agent function; the retry feedback is added by the prompt loader."""

    def writer_agent(state: GraphState) -> AgentOutput:
        template = load_prompt("writer")
        rules = platform_rules()
        banned = ", ".join(banned_words())
        feedback = (state.get("feedback") or {}).get(STEP_KEY)
        drafts = dict(state.get("drafts") or {})
        platforms = target_platforms(state)
        languages: dict[str, str] = {}

        for platform in platforms:
            messages = template.messages(
                feedback=feedback,
                platform=platform,
                language=state["language"],
                brand_voice=state.get("brand_voice"),
                banned_words=banned,
                platform_rules=to_json(dict(platform_rule(rules, platform))),
                strategy=to_json(dict(require_strategy(state, platform))),
                plan=to_json(state.get("plan")),
                research=to_json(state.get("research")),
            )
            reply = generate_structured(
                WriterOutput, messages, temperature=WRITER_TEMPERATURE
            )
            if reply.refused:
                reason = _clip(reply.reason or DEFAULT_REFUSAL, MAX_REFUSAL_CHARS)
                return AgentOutput(
                    update={},
                    prompt_version=template.version,
                    summary={"platforms": platforms, "refused_platform": platform},
                    refused_reason=f"{platform}: {reason}",
                )
            draft = dict(drafts.get(platform) or {})
            draft["caption"] = reply.caption
            if reply.title:
                draft["title"] = reply.title
            else:
                draft.pop("title", None)
            drafts[platform] = draft  # type: ignore[assignment]
            languages[platform] = reply.language

        return AgentOutput(
            update={"drafts": drafts},
            prompt_version=template.version,
            summary={
                "platforms": platforms,
                "languages": languages,
                "caption_chars": {p: len(drafts[p]["caption"]) for p in platforms},
            },
        )

    return writer_agent


def _text_issues(platform: str, caption: str, rule: Mapping[str, Any]) -> list[Issue]:
    """Mechanical checks on one caption (everything except banned words)."""
    issues: list[Issue] = []
    limit = rule.get("caption_max", rule.get("description_max"))
    if is_number(limit) and len(caption) > limit:
        issues.append(
            error(
                "caption_too_long",
                f"{platform}: the caption has {len(caption)} characters but the "
                f"limit is {limit}. Shorten it.",
            )
        )
    if _HASHTAG_IN_TEXT.search(caption):
        issues.append(
            error(
                "hashtag_in_caption",
                f"{platform}: remove the hashtags from the caption; "
                "another step adds them.",
            )
        )
    if _URL.search(caption):
        issues.append(
            error("raw_url", f"{platform}: remove the web address from the caption.")
        )
    if _PLACEHOLDER.search(caption):
        issues.append(
            error(
                "placeholder_left",
                f"{platform}: replace the unfilled placeholder (like [brand] or "
                "double curly braces) with real text.",
            )
        )
    return issues


def _length_issue(
    platform: str, caption: str, strategy: Mapping[str, Any]
) -> list[Issue]:
    target = strategy.get("length_target")
    if not is_number(target) or target < 1:
        return []
    low = math.ceil(target * (1 - LENGTH_TOLERANCE))
    high = math.floor(target * (1 + LENGTH_TOLERANCE))
    if low <= len(caption) <= high:
        return []
    direction = "Shorten" if len(caption) > high else "Lengthen"
    return [
        error(
            "caption_off_target",
            f"{platform}: the caption has {len(caption)} characters; the strategy "
            f"asks for about {target:g} ({low} to {high}). {direction} it.",
        )
    ]


def _title_issues(platform: str, title: Any, rule: Mapping[str, Any]) -> list[Issue]:
    has_title = isinstance(title, str) and bool(title.strip())
    if platform != "youtube":
        if has_title:
            return [
                error(
                    "title_not_allowed",
                    f"{platform}: leave the title empty (null); only YouTube has one.",
                )
            ]
        return []
    if not has_title:
        return [error("title_missing", "youtube: write a title.")]
    limit = rule.get("title_max")
    if is_number(limit) and len(title) > limit:
        return [
            error(
                "title_too_long",
                f"youtube: the title has {len(title)} characters but the limit "
                f"is {limit}. Shorten it.",
            )
        ]
    return []


def make_check_writer(
    platform_rules: Callable[[], PlatformRules] = default_platform_rules,
    banned_words: BannedWords = no_banned_words,
) -> Callable[[GraphState], CheckResult]:
    """Rule checks (AGENT_SPECS 4.1): caption present, within the platform limit and
    +-30% of ``length_target``, no banned words, no hashtags, raw URLs or
    placeholders in the body, and a title only (and always) for YouTube.

    The hook is the caption's first line, so a non-empty caption has one. Rules,
    strategy and banned words are read on every run.
    """

    def check(state: GraphState) -> CheckResult:
        drafts = state.get("drafts") or {}
        strategy = state.get("strategy") or {}
        rules = platform_rules()
        banned = banned_words()
        issues: list[Issue] = []

        for platform in target_platforms(state):
            draft = drafts.get(platform) or {}
            rule = platform_rule(rules, platform)
            caption = draft.get("caption")
            title = draft.get("title")
            if not isinstance(caption, str) or not caption.strip():
                issues.append(error("caption_empty", f"{platform}: write the caption."))
            else:
                issues += _text_issues(platform, caption, rule)
                issues += _length_issue(platform, caption, strategy.get(platform) or {})
            issues += _title_issues(platform, title, rule)
            texts = [t for t in (caption, title) if isinstance(t, str)]
            if contains_banned_word(texts, banned):
                issues.append(
                    error(
                        "banned_word",
                        f"{platform}: the text uses a banned word. Rewrite it without.",
                    )
                )
        return CheckResult.from_issues(issues)

    return check


def make_writer_judge(
    platform_rules: Callable[[], PlatformRules] = default_platform_rules,
) -> Callable[[GraphState], CheckResult]:
    """LLM judge for the captions (rubric: AGENT_SPECS 4.1). It only evaluates.

    The research notes go into the rubric text: they are what claims are judged
    against, and the shared judge prompt has no input for them.
    """

    def judge(state: GraphState) -> CheckResult:
        platforms = target_platforms(state)
        rules = platform_rules()
        drafts = state.get("drafts") or {}
        written = {
            p: {
                key: drafts.get(p, {}).get(key)
                for key in ("caption", "title")
                if drafts.get(p, {}).get(key)
            }
            for p in platforms
        }
        strategy = state.get("strategy") or {}
        rubric = (
            f"{WRITER_RUBRIC}\n\nResearch notes (the only source of specific "
            f"facts):\n{to_json(state.get('research'))}"
        )
        messages = load_prompt("judge").messages(
            agent=STEP_KEY,
            language=state["language"],
            brand_voice=state.get("brand_voice"),
            rubric=rubric,
            platform_rules=to_json(
                {p: dict(platform_rule(rules, p)) for p in platforms}
            ),
            plan=to_json(state.get("plan")),
            strategy=to_json({p: strategy.get(p) for p in platforms}),
            output=to_json(written),
        )
        return generate_structured(CheckResult, messages, kind="fast", temperature=0.0)

    return judge


def make_writer_node(
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    judge_threshold: float = DEFAULT_JUDGE_PASS_THRESHOLD,
    use_judge: bool = True,
    platform_rules: Callable[[], PlatformRules] = default_platform_rules,
    banned_words: BannedWords = no_banned_words,
) -> CheckedNode:
    """The writer as a checked node (step key ``writer``).

    Limits come from ``system_settings``; wire it with ``add_checked_node``.
    """
    return make_checked_node(
        STEP_KEY,
        make_writer_agent(platform_rules, banned_words),
        make_check_writer(platform_rules, banned_words),
        max_retries,
        judge_fn=make_writer_judge(platform_rules) if use_judge else None,
        judge_threshold=judge_threshold,
    )
