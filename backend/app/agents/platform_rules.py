"""Per-platform rules used by checks (``AGENT_SPECS.md`` section 2).

The values are **starting values only**: they live in
``system_settings.platform_rules`` and an admin can change them. These
defaults are the fallback when the setting is missing. Verify every number
against the platform's current official documentation before relying on it.
"""

from collections.abc import Mapping
from typing import Any

from app.agents.state import CONTENT_TYPES

PlatformRules = Mapping[str, Mapping[str, Any]]

DEFAULT_PLATFORM_RULES: dict[str, dict[str, Any]] = {
    "facebook": {
        "caption_max": 63000,
        "hashtags_min": 0,
        "hashtags_max": 5,
        "image_aspect": ["1:1", "4:5", "16:9"],
        "video_aspect": ["16:9", "9:16", "1:1"],
    },
    "instagram": {
        "caption_max": 2200,
        "hashtags_min": 3,
        "hashtags_max": 15,
        "image_aspect": ["1:1", "4:5"],
        "video_aspect": ["9:16"],
        "video_max_seconds": 90,
    },
    "tiktok": {
        "caption_max": 2200,
        "hashtags_min": 2,
        "hashtags_max": 8,
        "video_aspect": ["9:16"],
        "video_max_seconds": 180,
    },
    "youtube": {
        "title_max": 100,
        "description_max": 5000,
        "tags_max_chars": 500,
        "hashtags_min": 0,
        "hashtags_max": 15,
        "video_aspect": ["16:9", "9:16"],
        "video_max_seconds": 180,
    },
}


def default_platform_rules() -> PlatformRules:
    """The code defaults (the stand-in for reading ``system_settings``)."""
    return DEFAULT_PLATFORM_RULES


def supported_content_types(rule: Mapping[str, Any]) -> frozenset[str]:
    """Content types one platform accepts.

    An explicit ``allowed_formats`` list wins. Otherwise a platform takes
    ``post`` if it has image rules (``image_aspect``) and ``video`` if it has
    video rules (``video_aspect``), as in ``AGENT_SPECS.md`` section 2.
    """
    explicit = rule.get("allowed_formats")
    if isinstance(explicit, list | tuple | set | frozenset):
        return frozenset(f for f in explicit if f in CONTENT_TYPES)
    types: set[str] = set()
    if rule.get("image_aspect"):
        types.add("post")
    if rule.get("video_aspect"):
        types.add("video")
    return frozenset(types)


def platform_supports(rules: PlatformRules, platform: str, content_type: str) -> bool:
    """True if ``platform`` has rules and accepts ``content_type``."""
    rule = rules.get(platform)
    return isinstance(rule, Mapping) and content_type in supported_content_types(rule)
