"""Helpers shared by the post-flow nodes (``AGENT_SPECS.md`` section 4)."""

import json
from collections.abc import Mapping
from typing import Any

from app.agents.checked import Issue
from app.agents.platform_rules import PlatformRules
from app.agents.state import GraphState

NO_VALUE = "none"


def target_platforms(state: GraphState) -> list[str]:
    """Distinct target platforms in a stable order (FR-5.5: one draft each)."""
    return sorted({account["platform"] for account in state["target_accounts"]})


def to_json(value: Any) -> str:
    """Prompt text for a structured value (``none`` when it is empty)."""
    if not value:
        return NO_VALUE
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def platform_rule(rules: PlatformRules, platform: str) -> Mapping[str, Any]:
    """The rules of one platform (empty if it has none: no bounds)."""
    return rules.get(platform) or {}


def is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def error(code: str, message: str) -> Issue:
    return Issue(code=code, message=message, severity="error")


def attempt_number(state: GraphState, key: str) -> int:
    """The number the checked node will give the attempt that is about to run."""
    previous = (state.get("attempt_info") or {}).get(key) or {}
    return int(previous.get("attempt", 0)) + 1


def require_strategy(state: GraphState, platform: str) -> Mapping[str, Any]:
    """The strategy of ``platform``; a missing one is a bug, not a retry.

    The strategist's rule check guarantees one strategy per target platform, so
    reaching a content node without it means the graph was wired wrongly.
    """
    entry = (state.get("strategy") or {}).get(platform)
    if not isinstance(entry, Mapping):
        raise ValueError(f"no strategy for platform {platform!r}; run the strategist")
    return entry
