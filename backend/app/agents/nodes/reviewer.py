"""``reviewer`` and the review gate: final review and fix routing (AGENT_SPECS 5).

* :func:`make_reviewer_node` is a checked node (step key ``reviewer``). Its agent
  runs the mechanical platform-fit rules in code (caption plus hashtags within the
  platform limit, hashtag count, one image, banned words) and asks the LLM for the
  rest (quality, accuracy, policy, brand voice, consistency). Both lists of issues
  are merged into one :class:`ReviewResult` stored in ``state["review"]``, so one
  cycle can fix everything at once. The check validates the *shape* of the review
  (known ``responsible_node``, known platform, the five scores) and retries the
  reviewer if it is wrong.
* :func:`make_review_gate` is a plain node that applies the review to the state:
  ``ok`` clears ``rerun_from``; errors under the cycle cap set ``rerun_from`` to the
  earliest responsible node, store each node's ``fix_hint`` in ``feedback[node]``,
  reset the retry counters of every node that will run again (AGENT_SPECS 5.2) and
  count the cycle; at the cap the remaining errors become warnings for the human.
* :func:`route_after_review` turns that into an edge: ``ok``, ``planner``,
  ``strategist`` or ``content`` (back to ``content_router``).
"""

import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.agents.checked import (
    DEFAULT_MAX_RETRIES,
    MAX_FEEDBACK_CHARS,
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
from app.agents.prompts import load_prompt
from app.agents.state import GraphState

STEP_KEY = "reviewer"
REVIEW_TEMPERATURE = 0.1  # AGENT_SPECS 1.4: judges / reviewer 0.0-0.2
DEFAULT_MAX_REVIEW_CYCLES = 2  # system_settings.max_review_cycles
DEFAULT_POLICY_FLOOR = 0.3
SCORE_KEYS = ("quality", "accuracy", "policy", "platform_fit", "brand_voice")
POST_NODES = ("writer", "image", "hashtag")
VIDEO_NODES = ("script", "storyboard", "assembly", "voice")
_SCENE = re.compile(r"scene:([1-9][0-9]*)")
_MAX_WARNING_CHARS = 300
NO_VALUE = "none"


class ReviewIssue(BaseModel):
    """One problem found by the reviewer (``AGENT_SPECS.md`` 5.1)."""

    model_config = ConfigDict(str_strip_whitespace=True)

    severity: Literal["error", "warning"]
    platform: str | None = None
    responsible_node: str
    message: str
    fix_hint: str


class ReviewResult(BaseModel):
    """The review of all drafts (``AGENT_SPECS.md`` 5.1)."""

    model_config = ConfigDict(str_strip_whitespace=True)

    ok: bool
    scores: dict[str, float]
    issues: list[ReviewIssue]
    warnings: list[str]


# --------------------------------------------------------------------------
# Flow order and the re-entry table (AGENT_SPECS 5.2)
# --------------------------------------------------------------------------


def valid_nodes(content_type: str) -> tuple[str, ...]:
    """Steps a reviewer issue may name for this content type (plus ``scene:<n>``)."""
    shared = ("planner", "strategist")
    return (*shared, *(POST_NODES if content_type == "post" else VIDEO_NODES))


def is_valid_node(node: Any, content_type: str) -> bool:
    if not isinstance(node, str):
        return False
    if node in valid_nodes(content_type):
        return True
    return content_type == "video" and _SCENE.fullmatch(node) is not None


def _rank(node: str) -> tuple[int, int]:
    """Position in the flow; scenes sort by number."""
    order = ("planner", "strategist", *POST_NODES, "script", "storyboard")
    if node in order:
        return (order.index(node), 0)
    match = _SCENE.fullmatch(node)
    if match:
        return (len(order), int(match.group(1)))
    return (len(order) + 1, ["assembly", "voice"].index(node))


def earliest_node(nodes: Sequence[str]) -> str:
    """The first of ``nodes`` in flow order."""
    return min(nodes, key=_rank)


def nodes_to_rerun(
    rerun_from: str,
    content_type: str,
    *,
    image_flagged: bool = False,
    scene_keys: Sequence[str] = (),
) -> list[str]:
    """Step keys that run again when the flow re-enters at ``rerun_from``.

    ``scene_keys`` are the scene keys seen so far (``scene:1``...). ``researcher``
    is listed for a planner re-entry because the graph runs it again then.
    """
    content = list(POST_NODES if content_type == "post" else VIDEO_NODES)
    if content_type == "video":
        content = ["script", "storyboard", *scene_keys, "assembly", "voice"]
    if rerun_from == "planner":
        return ["planner", "researcher", "strategist", *content]
    if rerun_from == "strategist":
        return ["strategist", *content]
    if content_type == "post":
        if rerun_from == "writer":
            return ["writer", "hashtag", *(["image"] if image_flagged else [])]
        return [rerun_from]
    if rerun_from in ("script", "storyboard"):
        return content[content.index(rerun_from) :]
    if rerun_from in ("assembly", "voice"):
        return content[content.index(rerun_from) :]
    return [rerun_from, "assembly", "voice"]  # scene:<n>


# --------------------------------------------------------------------------
# Mechanical platform-fit rules (post flow)
# --------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _issue(platform: str, node: str, message: str, hint: str) -> ReviewIssue:
    return ReviewIssue(
        severity="error",
        platform=platform,
        responsible_node=node,
        message=message,
        fix_hint=hint,
    )


def rule_issues(
    state: GraphState,
    rules: PlatformRules,
    banned: Sequence[str],
) -> list[ReviewIssue]:
    """Platform-fit problems found without an LLM (posts only; video rules: B4)."""
    if state.get("content_type") != "post":
        return []
    drafts = state.get("drafts") or {}
    issues: list[ReviewIssue] = []
    for platform in sorted({a["platform"] for a in state["target_accounts"]}):
        draft = drafts.get(platform) or {}
        rule = rules.get(platform) or {}
        caption = draft.get("caption")
        if not isinstance(caption, str) or not caption.strip():
            issues.append(
                _issue(platform, "writer", f"{platform} has no caption.", "Write it.")
            )
            continue
        tags = [t for t in draft.get("hashtags") or [] if isinstance(t, str)]
        total = caption + ("\n\n" + " ".join(tags) if tags else "")
        limit = rule.get("caption_max", rule.get("description_max"))
        if _is_number(limit) and len(total) > limit:
            over = len(total) - limit
            issues.append(
                _issue(
                    platform,
                    "writer",
                    f"{platform}: caption and hashtags together have {len(total)} "
                    f"characters; the limit is {limit}.",
                    f"Shorten the caption by at least {over} characters.",
                )
            )
        low, high = rule.get("hashtags_min"), rule.get("hashtags_max")
        if (_is_number(low) and len(tags) < low) or (
            _is_number(high) and len(tags) > high
        ):
            issues.append(
                _issue(
                    platform,
                    "hashtag",
                    f"{platform}: {len(tags)} hashtags; the allowed range is "
                    f"{low if _is_number(low) else 0} to "
                    f"{high if _is_number(high) else 'no maximum'}.",
                    "Return a number of hashtags inside that range.",
                )
            )
        images = [
            m
            for m in draft.get("media") or []
            if isinstance(m, Mapping) and m.get("type") == "image"
        ]
        if len(images) != 1:
            issues.append(
                _issue(
                    platform,
                    "image",
                    f"{platform}: expected one image, found {len(images)}.",
                    "Create exactly one image.",
                )
            )
        texts = [caption, draft.get("title") or "", *tags]
        if contains_banned_word([t for t in texts if isinstance(t, str)], banned):
            issues.append(
                _issue(
                    platform,
                    "writer",
                    f"{platform}: the text uses a banned word (policy).",
                    "Rewrite the caption, title and hashtags without banned words.",
                )
            )
    return issues


# --------------------------------------------------------------------------
# The reviewer (checked node)
# --------------------------------------------------------------------------


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True) if value else NO_VALUE


def _drafts_for_review(state: GraphState) -> dict[str, Any]:
    """The drafts as the reviewer should see them (no storage keys)."""
    shown: dict[str, Any] = {}
    for platform, draft in (state.get("drafts") or {}).items():
        entry = {
            k: draft.get(k) for k in ("caption", "title", "hashtags") if draft.get(k)
        }
        media = []
        for item in draft.get("media") or []:
            if isinstance(item, Mapping):
                meta = item.get("meta") or {}
                media.append(
                    {
                        "type": item.get("type"),
                        **{
                            k: meta.get(k)
                            for k in (
                                "prompt",
                                "alt_text",
                                "aspect_ratio",
                                "width",
                                "height",
                            )
                            if meta.get(k) is not None
                        },
                    }
                )
        if media:
            entry["media"] = media
        shown[platform] = entry
    return shown


def make_reviewer_agent(
    platform_rules: Callable[[], PlatformRules] = default_platform_rules,
    banned_words: BannedWords = no_banned_words,
    policy_floor: float = DEFAULT_POLICY_FLOOR,
) -> Callable[[GraphState], AgentOutput]:
    """Build the agent function; the retry feedback is added by the prompt loader."""

    def reviewer_agent(state: GraphState) -> AgentOutput:
        template = load_prompt("reviewer")
        rules = platform_rules()
        banned = list(banned_words())
        platforms = sorted({a["platform"] for a in state["target_accounts"]})
        messages = template.messages(
            feedback=(state.get("feedback") or {}).get(STEP_KEY),
            content_type=state["content_type"],
            language=state["language"],
            brand_voice=state.get("brand_voice"),
            banned_words=", ".join(banned),
            platform_rules=_json({p: dict(rules.get(p) or {}) for p in platforms}),
            plan=_json(state.get("plan")),
            strategy=_json(state.get("strategy")),
            research=_json(state.get("research")),
            drafts=_json(_drafts_for_review(state)),
        )
        reply = generate_structured(
            ReviewResult, messages, kind="text", temperature=REVIEW_TEMPERATURE
        )
        issues = [*rule_issues(state, rules, banned), *reply.issues]
        result = ReviewResult(
            ok=not any(i.severity == "error" for i in issues),
            scores=reply.scores,
            issues=issues,
            warnings=reply.warnings,
        )
        refused = None
        policy = result.scores.get("policy")
        if not result.ok and _is_number(policy) and policy < policy_floor:
            first = next(i for i in issues if i.severity == "error")
            refused = f"policy: {first.message}"
        return AgentOutput(
            update={"review": result.model_dump()},
            prompt_version=template.version,
            summary={
                "ok": result.ok,
                "errors": sum(i.severity == "error" for i in issues),
                "rule_errors": len(issues) - len(reply.issues),
                "scores": result.scores,
            },
            refused_reason=refused,
        )

    return reviewer_agent


def make_check_review() -> Callable[[GraphState], CheckResult]:
    """Shape checks on the stored review (the reviewer is retried if they fail):
    every issue names a step that exists for this content type and a target
    platform (or null), error issues have a message and a ``fix_hint``, and
    ``scores`` has exactly the five keys with values from 0 to 1."""

    def check(state: GraphState) -> CheckResult:
        review = state.get("review") or {}
        content_type = state["content_type"]
        targets = {a["platform"] for a in state["target_accounts"]}
        issues: list[Issue] = []

        def bad(code: str, message: str) -> None:
            issues.append(Issue(code=code, message=message, severity="error"))

        scores = review.get("scores")
        valid_scores = (
            isinstance(scores, Mapping)
            and set(scores) == set(SCORE_KEYS)
            and all(_is_number(v) and 0 <= v <= 1 for v in scores.values())
        )
        if not valid_scores:
            bad(
                "scores_invalid",
                f"scores must have exactly these keys, each from 0 to 1: "
                f"{', '.join(SCORE_KEYS)}.",
            )
        allowed = ", ".join([*valid_nodes(content_type)]) + (
            ", scene:<n>" if content_type == "video" else ""
        )
        for item in review.get("issues") or []:
            node = item.get("responsible_node")
            if not is_valid_node(node, content_type):
                bad(
                    "responsible_node_invalid",
                    f"responsible_node '{node}' is not a step of this flow; use one "
                    f"of: {allowed}.",
                )
            platform = item.get("platform")
            if platform is not None and platform not in targets:
                bad(
                    "platform_unknown",
                    f"platform '{platform}' is not a target; use "
                    f"{', '.join(sorted(targets))} or null.",
                )
            if item.get("severity") == "error" and not (
                str(item.get("message") or "").strip()
                and str(item.get("fix_hint") or "").strip()
            ):
                bad("issue_incomplete", "Every error needs a message and a fix_hint.")
        return CheckResult.from_issues(issues)

    return check


def make_reviewer_node(
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    platform_rules: Callable[[], PlatformRules] = default_platform_rules,
    banned_words: BannedWords = no_banned_words,
    policy_floor: float = DEFAULT_POLICY_FLOOR,
) -> CheckedNode:
    """The reviewer as a checked node (step key ``reviewer``, no separate judge)."""
    return make_checked_node(
        STEP_KEY,
        make_reviewer_agent(platform_rules, banned_words, policy_floor),
        make_check_review(),
        max_retries,
    )


# --------------------------------------------------------------------------
# The gate and the routing edge
# --------------------------------------------------------------------------


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def make_review_gate(
    max_review_cycles: int = DEFAULT_MAX_REVIEW_CYCLES,
) -> Callable[[GraphState], dict[str, Any]]:
    """The node after the reviewer: decide ok / fix / cap and update the state."""
    if max_review_cycles < 0:
        raise ValueError("max_review_cycles must be >= 0")

    def review_gate(state: GraphState) -> dict[str, Any]:
        review = dict(state.get("review") or {})
        errors = [i for i in review.get("issues") or [] if i.get("severity") == "error"]
        cycles = int(state.get("review_cycles") or 0)
        if not errors:
            return {"rerun_from": None}
        if cycles >= max_review_cycles:
            extra = [
                _clip(
                    f"{i.get('platform') or 'all'}: {i.get('message')}",
                    _MAX_WARNING_CHARS,
                )
                for i in errors
            ]
            review["warnings"] = [*(review.get("warnings") or []), *extra]
            return {"review": review, "rerun_from": None}

        content_type = state["content_type"]
        by_node: dict[str, list[str]] = {}
        for item in errors:
            by_node.setdefault(item["responsible_node"], []).append(
                str(item["fix_hint"])
            )
        start = earliest_node(list(by_node))
        retries = dict(state.get("retries") or {})
        scene_keys = sorted(k for k in retries if _SCENE.fullmatch(k))
        again = nodes_to_rerun(
            start,
            content_type,
            image_flagged="image" in by_node,
            scene_keys=[*scene_keys, *(n for n in by_node if _SCENE.fullmatch(n))],
        )
        feedback = dict(state.get("feedback") or {})
        for node, hints in by_node.items():
            feedback[node] = _clip(" ".join(hints), MAX_FEEDBACK_CHARS)
        # Every flagged step runs again, even a later one the restart would skip.
        for node in dict.fromkeys([*again, *by_node]):
            retries[node] = 0
        return {
            "rerun_from": start,
            "review_cycles": cycles + 1,
            "feedback": feedback,
            "retries": retries,
            "status": "generating",
        }

    return review_gate


def route_after_review(
    state: GraphState,
) -> Literal["ok", "planner", "strategist", "content"]:
    """Where to go after the gate: onwards (``ok``) or back to the step to redo."""
    start = state.get("rerun_from")
    if not start:
        return "ok"
    if start in ("planner", "strategist"):
        return start  # type: ignore[return-value]
    return "content"


def route_after_content(state: GraphState) -> Literal["review", "failed"]:
    """After a content subgraph: review it, unless a human aborted a step there."""
    return "failed" if state.get("status") == "failed" else "review"
