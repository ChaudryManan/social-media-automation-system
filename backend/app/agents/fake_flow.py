"""Scripted fake-LLM replies for a whole post run (used by the dev runner).

The fake provider answers nothing by itself (``FakeScript``). ``post_flow_replies``
returns a function for ``FakeScript.set_default`` that recognises each prompt by its
role line and answers with a valid, rule-conforming JSON reply built from the topic,
so ``python -m app.agents.run`` works fully offline. Replies are deterministic.
"""

import re
from collections.abc import Callable
from typing import Any

from app.agents.llm import FakeCall
from app.agents.platform_rules import default_platform_rules

CAPTION_CHARS = 600  # the strategy's length target; the check allows +-30 %
SCORES = dict.fromkeys(
    ("quality", "accuracy", "policy", "platform_fit", "brand_voice"), 0.9
)
GENERIC_TAGS = (
    "socialmedia",
    "tips",
    "daily",
    "learn",
    "community",
    "ideas",
    "trending",
    "inspiration",
    "howto",
    "today",
    "growth",
    "habits",
    "lifestyle",
    "guide",
    "explore",
)
# (text found in the system prompt, handler name), most specific first.
_ROLES = (
    ("strict quality judge", "judge"),
    ("final reviewer", "reviewer"),
    ("content planner", "planner"),
    ("research assistant", "queries"),
    ("research analyst", "notes"),
    ("social media strategist", "strategy"),
    ("copywriter", "writer"),
    ("art director", "image"),
    ("hashtag specialist", "hashtag"),
)


def _field(user: str, label: str) -> str:
    match = re.search(rf"{re.escape(label)}:\s*(.+)", user)
    return match.group(1).strip() if match else ""


def _sentence(topic: str) -> str:
    return f"{topic.strip().rstrip('.!?')} is worth a closer look."


def _caption(topic: str) -> str:
    """Whole sentences up to the length target (never cut mid-sentence)."""
    sentence = _sentence(topic)[:200]
    text = sentence
    while len(text) + 1 + len(sentence) <= CAPTION_CHARS:
        text += " " + sentence
    return text


def _hashtags(topic: str, count: int) -> list[str]:
    words = [w for w in re.findall(r"[^\W_]{3,}", topic.casefold()) if not w.isdigit()]
    tags: list[str] = []
    for word in [*words, *GENERIC_TAGS]:
        tag = "#" + word[:30]
        if tag not in tags:
            tags.append(tag)
    return tags[:count]


def _strategy(platforms: list[str]) -> dict[str, Any]:
    rules = default_platform_rules()
    reply: dict[str, Any] = {}
    for platform in platforms:
        rule = rules.get(platform) or {}
        low, high = rule.get("hashtags_min") or 0, rule.get("hashtags_max") or 5
        reply[platform] = {
            "platform": platform,
            "tone": "friendly and clear",
            "structure": "hook line, short paragraphs, call to action",
            "length_target": CAPTION_CHARS,
            "cta": "Tell us what you think",
            "hashtag_count": max(low, min(high, 5)),
            "media_notes": "one clear, bright photo",
            "posting_notes": "post in the morning",
            "video": None,
        }
    return reply


def post_flow_replies(
    topic: str, language: str, platforms: list[str]
) -> Callable[[FakeCall], dict[str, Any]]:
    """A responder for ``FakeScript.set_default`` that runs a whole post flow."""

    def responder(call: FakeCall) -> dict[str, Any]:
        system, user = call.messages[0].content, call.messages[-1].content
        role = next((name for text, name in _ROLES if text in system), None)
        if role is None:
            raise ValueError("fake flow: unknown prompt role")
        if role == "judge":
            return {"ok": True, "score": 0.9, "issues": [], "feedback": ""}
        if role == "reviewer":
            return {"ok": True, "scores": SCORES, "issues": [], "warnings": []}
        if role == "planner":
            return {
                "angle": _sentence(topic),
                "audience": "curious people who follow the brand",
                "goal": "awareness",
                "key_messages": ["Start small", "Make it a daily habit"],
                "tone": "friendly and clear",
                "cta": "Tell us what you think",
                "format_notes": "short image post",
                "must_avoid": ["health or income guarantees"],
                "risks": ["claims need a source"],
                "language": language,
            }
        if role == "queries":
            return {"queries": [topic[:100]]}
        if role == "notes":
            return {
                "facts": [],
                "trends": [],
                "angles": [],
                "cautions": ["Offline run: no sources were checked."],
                "grounded": False,
            }
        if role == "strategy":
            return _strategy(platforms)
        if role == "writer":
            return {
                "caption": _caption(topic),
                "title": None,
                "hook": _sentence(topic),
                "cta": "Tell us what you think",
                "language": language,
                "refused": False,
                "reason": None,
            }
        if role == "image":
            return {
                "prompt": f"A bright, clean photo that illustrates: {topic[:150]}",
                "negative_prompt": "text, logos, watermarks",
                "aspect_ratio": "1:1",
                "alt_text": f"Illustration for {topic[:150]}",
                "style_notes": "bright, natural light",
            }
        wanted = _field(user, "Number of hashtags wanted")
        return {"hashtags": _hashtags(topic, int(wanted) if wanted.isdigit() else 5)}

    return responder
