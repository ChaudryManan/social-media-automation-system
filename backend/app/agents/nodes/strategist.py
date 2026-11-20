"""``strategist``: one strategy per target platform (``AGENT_SPECS.md`` 3.4).

The LLM adapts the content plan to every distinct target platform, using the
research notes, the workspace brand voice and the platform rules. The result is
stored in ``state["strategy"]`` as ``{platform: PlatformStrategy.model_dump()}``.

The step is a checked node (``checked.make_checked_node``). Like the planner,
the Pydantic models enforce only the shape; every limit (one entry per target,
lengths, hashtag range, aspect ratios, durations, scene count) is a rule check,
so a violation produces precise retry feedback.
"""

import json
from collections.abc import Callable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, RootModel

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
from app.agents.platform_rules import PlatformRules, default_platform_rules
from app.agents.prompts import load_prompt
from app.agents.state import GraphState

STEP_KEY = "strategist"
STRATEGY_TEMPERATURE = 0.5  # AGENT_SPECS 1.4: structured, stable
DEFAULT_MAX_SCENES = 5
MIN_SCENES = 3
NO_VALUE = "none"

STRATEGIST_RUBRIC = """\
Score from 0 to 1 on these three points, equally weighted:
1. Platform fit: each strategy suits its platform (style, length, structure, media
   direction) and the platform rules.
2. Consistency with the plan: angle, goal, audience and key messages are kept, and the
   strategies do not contradict each other.
3. Brand-voice fit: the tone follows the brand voice (the plan's tone if the brand
   voice is "none").
Report an error issue for invented facts or statistics, advice that ignores
research that is not grounded, and a strategy that ignores the plan."""


class VideoStrategy(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    format: Literal["short", "long"]
    aspect_ratio: str  # must be allowed for the platform (rule check)
    target_duration_s: int  # within the platform maximum (rule check)
    scene_count: int  # MIN_SCENES..max_scenes (rule check)
    style: str  # visual style guide for consistency


class PlatformStrategy(BaseModel):
    """One platform's strategy (``AGENT_SPECS.md`` 3.4)."""

    model_config = ConfigDict(str_strip_whitespace=True)

    platform: str
    tone: str
    structure: str  # e.g. "hook line, 3 short paragraphs, CTA"
    length_target: int  # characters for posts, seconds for video
    cta: str | None = None
    hashtag_count: int  # within platform min/max (rule check)
    media_notes: str  # image/video direction
    posting_notes: str
    video: VideoStrategy | None = None  # only if content_type == "video"


class StrategyReply(RootModel[dict[str, PlatformStrategy]]):
    """The strategist's whole reply: an object keyed by platform name."""


def _platforms(state: GraphState) -> list[str]:
    """Distinct target platforms in a stable order."""
    return sorted({account["platform"] for account in state["target_accounts"]})


def _json(value: Any) -> str:
    if not value:
        return NO_VALUE
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _relevant_rules(rules: PlatformRules, platforms: list[str]) -> dict[str, Any]:
    return {p: dict(rules[p]) for p in platforms if p in rules}


def make_strategy_agent(
    platform_rules: Callable[[], PlatformRules] = default_platform_rules,
) -> Callable[[GraphState], AgentOutput]:
    """Build the agent function; the retry feedback is added by the prompt loader."""

    def strategy_agent(state: GraphState) -> AgentOutput:
        platforms = _platforms(state)
        template = load_prompt("strategist")
        messages = template.messages(
            feedback=(state.get("feedback") or {}).get(STEP_KEY),
            content_type=state["content_type"],
            platforms=", ".join(platforms),
            language=state["language"],
            brand_voice=state.get("brand_voice"),
            platform_rules=_json(_relevant_rules(platform_rules(), platforms)),
            plan=_json(state.get("plan")),
            research=_json(state.get("research")),
        )
        reply = generate_structured(
            StrategyReply, messages, temperature=STRATEGY_TEMPERATURE
        )
        strategy = {name: item.model_dump() for name, item in reply.root.items()}
        return AgentOutput(
            update={"strategy": strategy},
            prompt_version=template.version,
            summary={
                "platforms": sorted(strategy),
                "content_type": state["content_type"],
            },
        )

    return strategy_agent


def _error(code: str, message: str) -> Issue:
    return Issue(code=code, message=message, severity="error")


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _check_length(
    name: str, item: Mapping[str, Any], rule: Mapping[str, Any], content_type: str
) -> list[Issue]:
    length = item.get("length_target")
    if not _is_number(length) or length < 1:
        return [_error("length_invalid", f"{name}: length_target must be at least 1.")]
    if content_type == "video":
        limit, unit = rule.get("video_max_seconds"), "seconds"
    else:
        limit, unit = rule.get("caption_max", rule.get("description_max")), "characters"
    if _is_number(limit) and length > limit:
        return [
            _error(
                "length_over_limit",
                f"{name}: length_target {length} is over the platform limit of "
                f"{limit} {unit}.",
            )
        ]
    return []


def _check_hashtags(
    name: str, item: Mapping[str, Any], rule: Mapping[str, Any]
) -> list[Issue]:
    count = item.get("hashtag_count")
    low, high = rule.get("hashtags_min"), rule.get("hashtags_max")
    if not _is_number(count) or count < 0:
        return [_error("hashtag_count_invalid", f"{name}: hashtag_count is invalid.")]
    if (_is_number(low) and count < low) or (_is_number(high) and count > high):
        return [
            _error(
                "hashtag_count_range",
                f"{name}: hashtag_count {count} must be between "
                f"{low if _is_number(low) else 0} and "
                f"{high if _is_number(high) else 'the platform maximum'}.",
            )
        ]
    return []


def _check_video(
    name: str,
    video: Any,
    rule: Mapping[str, Any],
    max_scenes: int,
) -> list[Issue]:
    if not isinstance(video, Mapping):
        return [
            _error(
                "video_missing", f"{name}: content type is video, add a video object."
            )
        ]
    issues: list[Issue] = []
    allowed = rule.get("video_aspect")
    if isinstance(allowed, list) and video.get("aspect_ratio") not in allowed:
        issues.append(
            _error(
                "aspect_ratio_not_allowed",
                f"{name}: aspect_ratio '{video.get('aspect_ratio')}' is not allowed; "
                f"use one of {', '.join(allowed)}.",
            )
        )
    duration = video.get("target_duration_s")
    limit = rule.get("video_max_seconds")
    if not _is_number(duration) or duration < 1:
        issues.append(
            _error("duration_invalid", f"{name}: target_duration_s must be at least 1.")
        )
    elif _is_number(limit) and duration > limit:
        issues.append(
            _error(
                "duration_over_limit",
                f"{name}: target_duration_s {duration} is over the platform maximum "
                f"of {limit} seconds.",
            )
        )
    scenes = video.get("scene_count")
    if not _is_number(scenes) or not MIN_SCENES <= scenes <= max_scenes:
        issues.append(
            _error(
                "scene_count_range",
                f"{name}: scene_count must be between {MIN_SCENES} and {max_scenes}.",
            )
        )
    if not str(video.get("style") or "").strip():
        issues.append(
            _error("style_empty", f"{name}: give the video a visual style guide.")
        )
    return issues


def make_check_strategy(
    platform_rules: Callable[[], PlatformRules] = default_platform_rules,
    max_scenes: int = DEFAULT_MAX_SCENES,
) -> Callable[[GraphState], CheckResult]:
    """Rule checks (AGENT_SPECS 3.4): one strategy per target platform, values within
    the platform rules, ``scene_count`` within the settings. Also: text fields
    filled in, ``video`` present exactly when the content type is video."""
    if max_scenes < MIN_SCENES:
        raise ValueError(f"max_scenes must be at least {MIN_SCENES}")

    def check(state: GraphState) -> CheckResult:
        strategy = state.get("strategy") or {}
        targets = _platforms(state)
        content_type = state["content_type"]
        rules = platform_rules()
        issues: list[Issue] = []

        missing = [p for p in targets if not isinstance(strategy.get(p), Mapping)]
        extra = sorted(p for p in strategy if p not in targets)
        if missing:
            issues.append(
                _error(
                    "platform_missing",
                    f"Add a strategy for: {', '.join(missing)}.",
                )
            )
        if extra:
            issues.append(
                _error(
                    "platform_unexpected",
                    f"Remove strategies for platforms that are not targets: "
                    f"{', '.join(extra)}.",
                )
            )

        for name in targets:
            item = strategy.get(name)
            if not isinstance(item, Mapping):
                continue
            rule = rules.get(name) or {}
            if item.get("platform") != name:
                issues.append(
                    _error(
                        "platform_field_mismatch",
                        f"{name}: the platform field must be '{name}'.",
                    )
                )
            for field in ("tone", "structure", "media_notes", "posting_notes"):
                if not str(item.get(field) or "").strip():
                    issues.append(_error("field_empty", f"{name}: fill in {field}."))
            issues += _check_length(name, item, rule, content_type)
            issues += _check_hashtags(name, item, rule)
            video = item.get("video")
            if content_type == "video":
                issues += _check_video(name, video, rule, max_scenes)
            elif video is not None:
                issues.append(
                    _error(
                        "video_not_allowed",
                        f"{name}: video must be null for a post.",
                    )
                )
        return CheckResult.from_issues(issues)

    return check


def make_strategy_judge(
    platform_rules: Callable[[], PlatformRules] = default_platform_rules,
) -> Callable[[GraphState], CheckResult]:
    """LLM judge for the strategy (rubric: AGENT_SPECS 3.4). It only evaluates."""

    def judge(state: GraphState) -> CheckResult:
        messages = load_prompt("judge").messages(
            agent=STEP_KEY,
            language=state["language"],
            brand_voice=state.get("brand_voice"),
            rubric=STRATEGIST_RUBRIC,
            platform_rules=_json(_relevant_rules(platform_rules(), _platforms(state))),
            plan=_json(state.get("plan")),
            strategy=NO_VALUE,  # the strategist is the step that creates it
            output=json.dumps(
                state.get("strategy"), ensure_ascii=False, sort_keys=True
            ),
        )
        return generate_structured(CheckResult, messages, kind="fast", temperature=0.0)

    return judge


def make_strategist_node(
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    judge_threshold: float = DEFAULT_JUDGE_PASS_THRESHOLD,
    use_judge: bool = True,
    max_scenes: int = DEFAULT_MAX_SCENES,
    platform_rules: Callable[[], PlatformRules] = default_platform_rules,
) -> CheckedNode:
    """The strategist as a checked node. Limits come from ``system_settings``.

    Wire it with ``checked.add_checked_node``.
    """
    return make_checked_node(
        STEP_KEY,
        make_strategy_agent(platform_rules),
        make_check_strategy(platform_rules, max_scenes),
        max_retries,
        judge_fn=make_strategy_judge(platform_rules) if use_judge else None,
        judge_threshold=judge_threshold,
    )
