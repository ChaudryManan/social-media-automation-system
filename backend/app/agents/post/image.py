"""``image``: one picture per platform (AGENT_SPECS 4.2).

Step A (LLM): an image prompt from the final caption. Step B: the image provider
makes the picture, the storage backend keeps it, and the draft gets a
``media`` entry ``{"type": "image", "storage_key": ..., "meta": {...}}``. The
``media_assets`` table does not exist until B3, so key and meta stay in the draft.

``size`` sent to the provider is a **request**: a real model decides the final
resolution. The rule checks therefore read the stored bytes back and measure the
real image (valid PNG/JPEG/WebP, size limit, allowed aspect ratio, minimum short
side). The LLM's aspect ratio is moved to an allowed one *before* an image is paid
for. Every attempt writes its own object (``image-<attempt>.<ext>``): a re-run of
the same attempt overwrites it, and a regeneration never overwrites an image that
an earlier content version still points to.
"""

import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

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
from app.agents.post.common import (
    attempt_number,
    error,
    platform_rule,
    require_strategy,
    target_platforms,
    to_json,
)
from app.agents.prompts import load_prompt
from app.agents.state import GraphState
from app.providers.image import (
    MAX_DIMENSION,
    ImageSize,
    detect_image_mime,
    get_image_provider,
    read_image_info,
)
from app.storage import StorageNotFoundError, get_storage_backend

STEP_KEY = "image"
IMAGE_TEMPERATURE = 0.7  # AGENT_SPECS 1.4: image prompt
DEFAULT_MIN_SHORT_SIDE = 1024  # AGENT_SPECS 4.2: pixels on the short side
DEFAULT_MAX_IMAGE_BYTES = 8 * 1024 * 1024
DEFAULT_ASPECT_RATIO = "1:1"
ASPECT_TOLERANCE = 0.03  # a real model's size may differ a little from the request
MAX_ALT_TEXT_CHARS = 200
_RATIO = re.compile(r"\s*(\d{1,3})\s*:\s*(\d{1,3})\s*")
_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}

IMAGE_RUBRIC = """\
You judge the image PROMPT, not a picture. Score from 0 to 1 on these points,
equally weighted:
1. Relevance: the described picture supports the caption and the content plan.
2. Brand fit: mood, style and setting suit the brand voice and the platform.
3. Safety and clarity: it is concrete and visual, asks for no text, letters or
   logos in the image, shows no real people, and the alt text describes it.
Report an error issue for a prompt unrelated to the caption, one that asks for
text or logos in the picture, and one that names real people or brands."""


class ImagePrompt(BaseModel):
    """The image agent's reply for one platform (``AGENT_SPECS.md`` 4.2, step A)."""

    model_config = ConfigDict(str_strip_whitespace=True)

    prompt: str = Field(min_length=1)  # concrete visual description
    negative_prompt: str | None = None
    aspect_ratio: str  # moved to an allowed one before generating
    alt_text: str  # accessibility text, <= 200 chars (rule check)
    style_notes: str


def parse_aspect_ratio(value: Any) -> tuple[int, int] | None:
    """``"4:5"`` -> ``(4, 5)``; ``None`` if it is not two positive integers."""
    match = _RATIO.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        return None
    width, height = int(match.group(1)), int(match.group(2))
    return (width, height) if width > 0 and height > 0 else None


def size_for_aspect_ratio(
    ratio: str, short_side: int = DEFAULT_MIN_SHORT_SIDE
) -> ImageSize:
    """The ``(width, height)`` to request for ``ratio``: ``short_side`` pixels on the
    short side. Raises ``ValueError`` for a bad ratio or a size over the provider's
    maximum."""
    parsed = parse_aspect_ratio(ratio)
    if parsed is None:
        raise ValueError(f"not an aspect ratio: {ratio!r}")
    ratio_w, ratio_h = parsed
    if ratio_w >= ratio_h:
        size = (round(short_side * ratio_w / ratio_h), short_side)
    else:
        size = (short_side, round(short_side * ratio_h / ratio_w))
    if max(size) > MAX_DIMENSION:
        raise ValueError(f"aspect ratio {ratio!r} needs more than {MAX_DIMENSION}px")
    return size


def _usable(ratio: str, short_side: int) -> bool:
    try:
        size_for_aspect_ratio(ratio, short_side)
    except ValueError:
        return False
    return True


def _log_ratio(ratio: str) -> float:
    width, height = parse_aspect_ratio(ratio) or (1, 1)
    return math.log(width / height)


def choose_aspect_ratio(
    requested: str, allowed: Sequence[str], short_side: int = DEFAULT_MIN_SHORT_SIDE
) -> str:
    """The ratio to request: ``requested`` if the platform allows it, else the
    allowed ratio nearest to it (the first one if ``requested`` is not a ratio).
    A platform without ``image_aspect`` rules takes ``requested`` if it is usable,
    else ``1:1``."""
    asked = requested.strip()
    options = [r for r in allowed if _usable(r, short_side)]
    if not options:
        return asked if _usable(asked, short_side) else DEFAULT_ASPECT_RATIO
    if asked in options:
        return asked
    if parse_aspect_ratio(asked) is None:
        return options[0]
    return min(options, key=lambda r: abs(_log_ratio(r) - _log_ratio(asked)))


def _ratio_allowed(width: int, height: int, allowed: Sequence[str]) -> bool:
    actual = width / height
    for ratio in allowed:
        parsed = parse_aspect_ratio(ratio)
        if parsed is None:
            continue
        wanted = parsed[0] / parsed[1]
        if abs(actual - wanted) / wanted <= ASPECT_TOLERANCE:
            return True
    return False


def _request_id(state: GraphState) -> str:
    request_id = state.get("request_id")
    if not request_id:
        raise ValueError("the image step needs state['request_id'] for storage keys")
    return str(request_id)


def make_image_agent(
    platform_rules: Callable[[], PlatformRules] = default_platform_rules,
    min_short_side: int = DEFAULT_MIN_SHORT_SIDE,
) -> Callable[[GraphState], AgentOutput]:
    """Build the agent function; the retry feedback is added by the prompt loader."""

    def image_agent(state: GraphState) -> AgentOutput:
        template = load_prompt("image")
        rules = platform_rules()
        feedback = (state.get("feedback") or {}).get(STEP_KEY)
        drafts = dict(state.get("drafts") or {})
        platforms = target_platforms(state)
        attempt = attempt_number(state, STEP_KEY)
        request_id = _request_id(state)
        provider = get_image_provider()
        storage = get_storage_backend()
        stored: dict[str, str] = {}

        for platform in platforms:
            caption = (drafts.get(platform) or {}).get("caption")
            if not caption:
                raise ValueError(
                    f"the image step needs the {platform} caption; run the writer"
                )
            allowed = list(platform_rule(rules, platform).get("image_aspect") or [])
            messages = template.messages(
                feedback=feedback,
                platform=platform,
                aspect_ratios=", ".join(allowed) or "any",
                language=state["language"],
                brand_voice=state.get("brand_voice"),
                caption=caption,
                strategy=to_json(dict(require_strategy(state, platform))),
                plan=to_json(state.get("plan")),
            )
            reply = generate_structured(
                ImagePrompt, messages, temperature=IMAGE_TEMPERATURE
            )

            ratio = choose_aspect_ratio(reply.aspect_ratio, allowed, min_short_side)
            size = size_for_aspect_ratio(ratio, min_short_side)
            # The provider has no negative-prompt input, so it goes into the text.
            text = reply.prompt
            if reply.negative_prompt:
                text = f"{text}\n\nAvoid: {reply.negative_prompt}"
            data = provider.generate(text, size)

            mime = detect_image_mime(data)
            key = (
                f"requests/{request_id}/{platform}/"
                f"image-{attempt}.{_EXTENSIONS.get(mime or '', 'bin')}"
            )
            storage.put(key, data, mime or "application/octet-stream")
            stored[platform] = key

            meta: dict[str, Any] = {
                "prompt": text,
                "aspect_ratio": ratio,
                "alt_text": reply.alt_text,
                "style_notes": reply.style_notes,
                "provider": provider.name,
                "size_bytes": len(data),
            }
            info = read_image_info(data)
            if info is not None:
                meta.update(mime_type=info.mime, width=info.width, height=info.height)
            draft = dict(drafts[platform])
            others = [
                m
                for m in draft.get("media") or []
                if not (isinstance(m, Mapping) and m.get("type") == "image")
            ]
            draft["media"] = [
                *others,
                {"type": "image", "storage_key": key, "meta": meta},
            ]
            drafts[platform] = draft  # type: ignore[assignment]

        return AgentOutput(
            update={"drafts": drafts},
            prompt_version=template.version,
            summary={"platforms": platforms, "storage_keys": stored},
        )

    return image_agent


def _images(draft: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    media = draft.get("media")
    if not isinstance(media, list):
        return []
    return [m for m in media if isinstance(m, Mapping) and m.get("type") == "image"]


def _file_issues(
    platform: str,
    data: bytes,
    allowed: Sequence[str],
    max_bytes: int,
    min_short_side: int,
) -> list[Issue]:
    info = read_image_info(data)
    if info is None:
        return [
            error(
                "image_invalid",
                f"{platform}: the file is not a valid PNG, JPEG or WebP image. "
                "Describe a simpler, concrete picture.",
            )
        ]
    issues: list[Issue] = []
    if len(data) > max_bytes:
        issues.append(
            error(
                "image_too_large",
                f"{platform}: the image is {len(data)} bytes but the limit is "
                f"{max_bytes}. Describe a simpler picture.",
            )
        )
    if min(info.width, info.height) < min_short_side:
        issues.append(
            error(
                "image_too_small",
                f"{platform}: the image is {info.width}x{info.height}; the short "
                f"side must be at least {min_short_side} pixels.",
            )
        )
    if allowed and not _ratio_allowed(info.width, info.height, allowed):
        issues.append(
            error(
                "image_aspect_not_allowed",
                f"{platform}: the image is {info.width}x{info.height}, which is not "
                f"one of the allowed ratios ({', '.join(allowed)}).",
            )
        )
    return issues


def make_check_image(
    platform_rules: Callable[[], PlatformRules] = default_platform_rules,
    max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
    min_short_side: int = DEFAULT_MIN_SHORT_SIDE,
) -> Callable[[GraphState], CheckResult]:
    """Rule checks (AGENT_SPECS 4.2): one stored image per platform that is a valid
    PNG, JPEG or WebP, under ``max_image_bytes``, at least ``min_short_side`` pixels
    on the short side, with an allowed aspect ratio; alt text present and at most
    200 characters. The size and ratio are read from the stored bytes.
    """

    def check(state: GraphState) -> CheckResult:
        drafts = state.get("drafts") or {}
        rules = platform_rules()
        issues: list[Issue] = []
        storage = get_storage_backend()

        for platform in target_platforms(state):
            images = _images(drafts.get(platform) or {})
            if len(images) != 1:
                issues.append(
                    error(
                        "image_missing",
                        f"{platform}: create exactly one image, found {len(images)}.",
                    )
                )
                continue
            item = images[0]
            alt = (item.get("meta") or {}).get("alt_text")
            if not isinstance(alt, str) or not alt.strip():
                issues.append(
                    error("alt_text_missing", f"{platform}: write the alt text.")
                )
            elif len(alt) > MAX_ALT_TEXT_CHARS:
                issues.append(
                    error(
                        "alt_text_too_long",
                        f"{platform}: the alt text has {len(alt)} characters; "
                        f"use at most {MAX_ALT_TEXT_CHARS}.",
                    )
                )
            key = item.get("storage_key")
            try:
                if not isinstance(key, str):
                    raise StorageNotFoundError
                data = storage.get(key)
            except StorageNotFoundError:
                issues.append(
                    error(
                        "image_missing",
                        f"{platform}: the stored image was not found; create it again.",
                    )
                )
                continue
            allowed = platform_rule(rules, platform).get("image_aspect") or []
            issues += _file_issues(
                platform, data, allowed, max_image_bytes, min_short_side
            )
        return CheckResult.from_issues(issues)

    return check


def make_image_judge(
    platform_rules: Callable[[], PlatformRules] = default_platform_rules,
) -> Callable[[GraphState], CheckResult]:
    """Judge of the image prompt against the caption (AGENT_SPECS 4.2: the text-only
    variant used when no vision model is configured). It only evaluates."""

    def judge(state: GraphState) -> CheckResult:
        platforms = target_platforms(state)
        rules = platform_rules()
        drafts = state.get("drafts") or {}
        strategy = state.get("strategy") or {}
        shown: dict[str, Any] = {}
        for platform in platforms:
            draft = drafts.get(platform) or {}
            meta = (_images(draft) or [{}])[0].get("meta") or {}
            shown[platform] = {
                "caption": draft.get("caption"),
                "image_prompt": meta.get("prompt"),
                "alt_text": meta.get("alt_text"),
                "style_notes": meta.get("style_notes"),
            }
        messages = load_prompt("judge").messages(
            agent=STEP_KEY,
            language=state["language"],
            brand_voice=state.get("brand_voice"),
            rubric=IMAGE_RUBRIC,
            platform_rules=to_json(
                {p: dict(platform_rule(rules, p)) for p in platforms}
            ),
            plan=to_json(state.get("plan")),
            strategy=to_json({p: strategy.get(p) for p in platforms}),
            output=json.dumps(shown, ensure_ascii=False, sort_keys=True),
        )
        return generate_structured(CheckResult, messages, kind="fast", temperature=0.0)

    return judge


def make_image_node(
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    judge_threshold: float = DEFAULT_JUDGE_PASS_THRESHOLD,
    use_judge: bool = True,
    platform_rules: Callable[[], PlatformRules] = default_platform_rules,
    max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
    min_short_side: int = DEFAULT_MIN_SHORT_SIDE,
) -> CheckedNode:
    """The image step as a checked node (step key ``image``).

    Limits come from ``system_settings``; wire it with ``add_checked_node``.
    """
    return make_checked_node(
        STEP_KEY,
        make_image_agent(platform_rules, min_short_side),
        make_check_image(platform_rules, max_image_bytes, min_short_side),
        max_retries,
        judge_fn=make_image_judge(platform_rules) if use_judge else None,
        judge_threshold=judge_threshold,
    )
