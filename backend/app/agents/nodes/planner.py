"""``planner``: turns the topic into one content plan (``AGENT_SPECS.md`` 3.2).

The planner is a checked step (``checked.make_checked_node``): it generates a
:class:`ContentPlan`, the rule checks verify it, an LLM judge scores it, and a
failure goes back to the planner with the checker's feedback. The result is
stored in ``state["plan"]`` as ``plan.model_dump()``.

``ContentPlan`` does not enforce the rule-checked limits itself (how many key
messages, empty angle): a schema failure would only trigger the silent
repair, while a rule-check failure gives the planner precise feedback and is
logged as an attempt. Only the shape (``goal`` must be one of five values)
is enforced by the schema.
"""

import json
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

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

STEP_KEY = "planner"
KEY_MESSAGES_MIN = 2
KEY_MESSAGES_MAX = 5
NO_VALUE = "none"

PLANNER_RUBRIC = """\
Score from 0 to 1 on these four points, equally weighted:
1. Relevance: the angle and the key messages are about the topic and serve the goal.
2. Clarity of angle: one clear main idea or hook, not a list of ideas.
3. Audience fit: the audience is specific and the tone and messages suit it.
4. Brand-voice fit: the plan follows the brand voice (a neutral, friendly tone
   if the brand voice is "none").
Report an error issue for invented data, statistics or quotes, and for a plan
that is off-topic."""


class ContentPlan(BaseModel):
    """The planner's output (``AGENT_SPECS.md`` 3.2)."""

    model_config = ConfigDict(str_strip_whitespace=True)

    angle: str  # the single main idea/hook
    audience: str
    goal: Literal["awareness", "engagement", "education", "promotion", "other"]
    key_messages: list[str]  # 2..5, checked by the rule check
    tone: str
    cta: str | None = None
    format_notes: str
    must_avoid: list[str] = Field(default_factory=list)  # sensitive topics, claims
    risks: list[str] = Field(default_factory=list)  # policy/accuracy risks
    language: str


def _platforms(state: GraphState) -> list[str]:
    """Distinct target platforms in a stable order."""
    return sorted({account["platform"] for account in state["target_accounts"]})


def _normalise_language(tag: str) -> str:
    return tag.strip().replace("_", "-").casefold()


def _options_text(options: dict[str, Any] | None) -> str:
    if not options:
        return NO_VALUE
    return json.dumps(options, ensure_ascii=False, sort_keys=True)


def plan_agent(state: GraphState) -> AgentOutput:
    """Generate the plan; the retry feedback (if any) is added by the prompt loader."""
    template = load_prompt("planner")
    messages = template.messages(
        feedback=(state.get("feedback") or {}).get(STEP_KEY),
        topic=state["topic"],
        content_type=state["content_type"],
        platforms=", ".join(_platforms(state)),
        language=state["language"],
        brand_voice=state.get("brand_voice"),
        options=_options_text(state.get("options")),
    )
    plan = generate_structured(ContentPlan, messages)
    return AgentOutput(
        update={"plan": plan.model_dump()},
        prompt_version=template.version,
        summary={
            "goal": plan.goal,
            "key_messages": len(plan.key_messages),
            "language": plan.language,
        },
    )


def _error(code: str, message: str) -> Issue:
    return Issue(code=code, message=message, severity="error")


def check_plan(state: GraphState) -> CheckResult:
    """Rule checks (AGENT_SPECS 3.2): 2 to 5 key messages, angle and audience
    filled in, language equal to the requested one."""
    plan = state.get("plan") or {}
    issues: list[Issue] = []

    if not str(plan.get("angle") or "").strip():
        issues.append(
            _error(
                "angle_empty", "Give the plan one clear angle (the main idea or hook)."
            )
        )
    if not str(plan.get("audience") or "").strip():
        issues.append(_error("audience_empty", "Name the target audience of the plan."))

    messages = [str(m).strip() for m in plan.get("key_messages") or []]
    filled = [m for m in messages if m]
    if len(filled) != len(messages):
        issues.append(_error("key_message_empty", "Remove empty key messages."))
    if not KEY_MESSAGES_MIN <= len(filled) <= KEY_MESSAGES_MAX:
        issues.append(
            _error(
                "key_messages_count",
                f"Give {KEY_MESSAGES_MIN} to {KEY_MESSAGES_MAX} key messages; "
                f"the plan has {len(filled)}.",
            )
        )

    requested = state["language"]
    if _normalise_language(str(plan.get("language") or "")) != _normalise_language(
        requested
    ):
        issues.append(
            _error(
                "language_mismatch",
                f"Write the plan in '{requested}' and set language to '{requested}'.",
            )
        )
    return CheckResult.from_issues(issues)


def make_plan_judge(
    platform_rules: Callable[[], PlatformRules] = default_platform_rules,
) -> Callable[[GraphState], CheckResult]:
    """LLM judge for the plan (rubric: AGENT_SPECS 3.2). It only evaluates."""

    def judge(state: GraphState) -> CheckResult:
        rules = platform_rules()
        relevant = {p: rules[p] for p in _platforms(state) if p in rules}
        messages = load_prompt("judge").messages(
            agent=STEP_KEY,
            language=state["language"],
            brand_voice=state.get("brand_voice"),
            rubric=PLANNER_RUBRIC,
            platform_rules=json.dumps(relevant, ensure_ascii=False, sort_keys=True),
            plan=NO_VALUE,  # the planner is the step that creates the plan
            strategy=NO_VALUE,
            output=json.dumps(state.get("plan"), ensure_ascii=False, sort_keys=True),
        )
        return generate_structured(CheckResult, messages, kind="fast")

    return judge


def make_planner_node(
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    judge_threshold: float = DEFAULT_JUDGE_PASS_THRESHOLD,
    use_judge: bool = True,
    platform_rules: Callable[[], PlatformRules] = default_platform_rules,
) -> CheckedNode:
    """The planner as a checked node. Limits come from ``system_settings``.

    Wire it with ``checked.add_checked_node``.
    """
    return make_checked_node(
        STEP_KEY,
        plan_agent,
        check_plan,
        max_retries,
        judge_fn=make_plan_judge(platform_rules) if use_judge else None,
        judge_threshold=judge_threshold,
    )
