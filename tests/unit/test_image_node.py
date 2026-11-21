"""Tests for the post-flow ``image`` node (B2-10): fake LLM, fake image provider
and a local storage directory. Nothing touches the network."""

import json
import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from app.agents.checked import StepRecord, add_checked_node
from app.agents.llm import FakeScript, LLMOutputError
from app.agents.post.image import (
    DEFAULT_MAX_IMAGE_BYTES,
    IMAGE_RUBRIC,
    ImagePrompt,
    choose_aspect_ratio,
    make_check_image,
    make_image_agent,
    make_image_judge,
    make_image_node,
    parse_aspect_ratio,
    size_for_aspect_ratio,
)
from app.agents.prompts import load_prompt
from app.agents.state import GraphState
from app.providers.image import (
    FakeImage,
    ImageConfigError,
    ImageProviderError,
    build_png,
    read_image_info,
)
from app.storage import StorageConfigError, get_storage_backend
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "AGENT_SPECS.md"
PROMPT = ROOT / "backend" / "app" / "agents" / "prompts" / "image.md"

pytestmark = pytest.mark.usefixtures("media_root", "fake_image")


def prompt_reply(**changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "prompt": "A steaming cup of green tea on a wooden table, soft morning light",
        "negative_prompt": None,
        "aspect_ratio": "4:5",
        "alt_text": "A cup of green tea on a table",
        "style_notes": "warm greens, calm mood",
    }
    data.update(changes)
    return data


def good_judge(score: float = 0.9) -> dict[str, Any]:
    return {"ok": True, "score": score, "issues": [], "feedback": ""}


def strat(platform: str) -> dict[str, Any]:
    return {
        "platform": platform,
        "tone": "warm",
        "structure": "hook",
        "length_target": 600,
        "cta": None,
        "hashtag_count": 3,
        "media_notes": "bright photo of tea",
        "posting_notes": "morning",
        "video": None,
    }


def state(**changes: Any) -> GraphState:
    base: dict[str, Any] = {
        "request_id": "r1",
        "workspace_id": "w1",
        "user_id": "u1",
        "topic": "Benefits of green tea",
        "content_type": "post",
        "target_accounts": [
            {"account_id": "a1", "platform": "instagram"},
            {"account_id": "a2", "platform": "facebook"},
            {"account_id": "a3", "platform": "instagram"},
        ],
        "publish_at": "2026-12-01T10:00:00Z",
        "brand_voice": "warm and simple",
        "language": "en",
        "plan": {"angle": "Tea is a daily habit"},
        "strategy": {"instagram": strat("instagram"), "facebook": strat("facebook")},
        "drafts": {
            "instagram": {"caption": "Instagram caption", "hashtags": ["#tea"]},
            "facebook": {"caption": "Facebook caption"},
        },
    }
    base.update(changes)
    return base  # type: ignore[return-value]


def store(key: str, data: bytes, mime: str = "image/png") -> str:
    get_storage_backend().put(key, data, mime)
    return key


def with_image(
    platform: str, data: bytes, *, key: str | None = None, **meta: Any
) -> dict[str, Any]:
    """A media entry whose bytes are already in storage."""
    key = store(key or f"requests/r1/{platform}/image-1.png", data)
    entry = {"alt_text": "A cup of tea", "prompt": "a cup of tea", **meta}
    return {"type": "image", "storage_key": key, "meta": entry}


def imaged(instagram: bytes | None = None, facebook: bytes | None = None) -> GraphState:
    """A state whose drafts hold valid stored images (Instagram 4:5, Facebook 1:1)."""
    base = state()
    drafts = base["drafts"]  # type: ignore[typeddict-item]
    drafts["instagram"]["media"] = [
        with_image("instagram", instagram or build_png(1024, 1280, (1, 2, 3)))
    ]
    drafts["facebook"]["media"] = [
        with_image("facebook", facebook or build_png(1024, 1024, (1, 2, 3)))
    ]
    return base


def codes(result: Any) -> list[str]:
    return [i.code for i in result.issues]


# --------------------------------------------------------------------------
# Aspect-ratio helpers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("4:5", (4, 5)),
        (" 16 : 9 ", (16, 9)),
        ("1:1", (1, 1)),
        ("21:9", (21, 9)),
        ("0:1", None),
        ("1:0", None),
        ("wide", None),
        ("4x5", None),
        ("4:5:6", None),
        ("-4:5", None),
        ("1000:1", None),
        ("", None),
        (None, None),
        (1.5, None),
    ],
)
def test_parse_aspect_ratio(value: Any, expected: Any) -> None:
    assert parse_aspect_ratio(value) == expected


@pytest.mark.parametrize(
    ("ratio", "size"),
    [
        ("1:1", (1024, 1024)),
        ("4:5", (1024, 1280)),
        ("5:4", (1280, 1024)),
        ("16:9", (1820, 1024)),
        ("9:16", (1024, 1820)),
        ("2:2", (1024, 1024)),
    ],
)
def test_size_has_1024_on_the_short_side(ratio: str, size: tuple[int, int]) -> None:
    assert size_for_aspect_ratio(ratio) == size


def test_the_short_side_is_a_parameter() -> None:
    assert size_for_aspect_ratio("4:5", 512) == (512, 640)


@pytest.mark.parametrize("ratio", ["wide", "0:5", "", "5:1"])
def test_a_bad_or_too_large_ratio_is_a_value_error(ratio: str) -> None:
    with pytest.raises(ValueError):
        size_for_aspect_ratio(ratio)


def test_a_ratio_that_needs_more_than_the_provider_maximum_is_refused() -> None:
    assert size_for_aspect_ratio("4:1") == (4096, 1024)
    with pytest.raises(ValueError, match="4096"):
        size_for_aspect_ratio("41:10")


@pytest.mark.parametrize(
    ("requested", "allowed", "expected"),
    [
        ("4:5", ["1:1", "4:5"], "4:5"),
        (" 4:5 ", ["1:1", "4:5"], "4:5"),
        ("16:9", ["1:1", "4:5"], "1:1"),  # nearest on a log scale
        ("9:16", ["1:1", "4:5"], "4:5"),
        ("2:2", ["4:5", "1:1"], "1:1"),  # same ratio written differently
        ("wide", ["1:1", "4:5"], "1:1"),  # not a ratio: the first allowed
        ("", ["4:5", "1:1"], "4:5"),
        ("7:9", ["bad", "1:1", "4:5"], "4:5"),  # unusable entries are ignored
    ],
)
def test_the_requested_ratio_is_moved_to_an_allowed_one(
    requested: str, allowed: list[str], expected: str
) -> None:
    assert choose_aspect_ratio(requested, allowed) == expected


@pytest.mark.parametrize(
    ("requested", "allowed", "expected"),
    [
        ("16:9", [], "16:9"),  # no rules: the model's choice
        ("wide", [], "1:1"),  # unusable: the default
        ("41:10", [], "1:1"),  # over the provider maximum
        ("4:5", ["bad", "worse"], "4:5"),  # no usable allowed ratio
    ],
)
def test_without_usable_rules_the_request_or_the_default_is_used(
    requested: str, allowed: list[Any], expected: str
) -> None:
    assert choose_aspect_ratio(requested, allowed) == expected


# --------------------------------------------------------------------------
# Model and the spec / prompt contract
# --------------------------------------------------------------------------


def test_model_fields_equal_the_spec_class() -> None:
    text = SPEC.read_text(encoding="utf-8")
    block = re.search(r"class ImagePrompt\(BaseModel\):\n(.*?)\n```", text, re.S)
    assert block
    spec = set(re.findall(r"^    (\w+):", block.group(1), re.M))

    assert set(ImagePrompt.model_fields) == spec


def test_prompt_example_fields_equal_the_model_fields() -> None:
    text = PROMPT.read_text(encoding="utf-8")
    example = json.loads(re.search(r"```json\n(.*?)```", text, re.S).group(1))  # type: ignore[union-attr]

    assert set(example) == set(ImagePrompt.model_fields)
    ImagePrompt.model_validate(example)


def test_a_full_reply_round_trips_and_the_negative_prompt_is_optional() -> None:
    assert ImagePrompt.model_validate(prompt_reply()).model_dump() == prompt_reply()
    data = prompt_reply()
    del data["negative_prompt"]

    assert ImagePrompt.model_validate(data).negative_prompt is None


@pytest.mark.parametrize("field", ["prompt", "aspect_ratio", "alt_text", "style_notes"])
def test_the_core_fields_are_required(field: str) -> None:
    data = prompt_reply()
    del data[field]

    with pytest.raises(ValidationError):
        ImagePrompt.model_validate(data)


@pytest.mark.parametrize("prompt", ["", "   \n"])
def test_a_blank_prompt_is_a_schema_error_so_the_reply_is_repaired(
    prompt: str,
) -> None:
    with pytest.raises(ValidationError):
        ImagePrompt.model_validate(prompt_reply(prompt=prompt))


def test_the_alt_text_limit_is_a_rule_check_not_a_schema_error() -> None:
    assert (
        len(ImagePrompt.model_validate(prompt_reply(alt_text="a" * 999)).alt_text)
        == 999
    )


# --------------------------------------------------------------------------
# image_agent
# --------------------------------------------------------------------------


def test_the_agent_makes_stores_and_records_one_image_per_distinct_platform(
    fake_llm: FakeScript, fake_image: FakeImage
) -> None:
    fake_llm.queue(prompt_reply(aspect_ratio="1:1"), prompt_reply(aspect_ratio="4:5"))

    result = make_image_agent()(state())

    # Three accounts, two platforms: two prompts and two images (FR-5.5).
    assert len(fake_llm.calls) == 2 and len(fake_image.calls) == 2
    assert [size for _, size in fake_image.calls] == [(1024, 1024), (1024, 1280)]
    assert all(c.kind == "text" and c.temperature == 0.7 for c in fake_llm.calls)
    drafts = result.update["drafts"]
    storage = get_storage_backend()
    for platform, size in (("facebook", (1024, 1024)), ("instagram", (1024, 1280))):
        (media,) = drafts[platform]["media"]
        assert media["type"] == "image"
        assert media["storage_key"] == f"requests/r1/{platform}/image-1.png"
        info = read_image_info(storage.get(media["storage_key"]))
        assert (info.width, info.height) == size  # type: ignore[union-attr]
    assert drafts["instagram"]["caption"] == "Instagram caption"  # kept
    assert drafts["instagram"]["hashtags"] == ["#tea"]
    assert result.prompt_version == load_prompt("image").version == "image.v1"
    assert result.summary == {
        "platforms": ["facebook", "instagram"],
        "storage_keys": {
            "facebook": "requests/r1/facebook/image-1.png",
            "instagram": "requests/r1/instagram/image-1.png",
        },
    }


def test_the_media_meta_describes_the_image_and_how_it_was_made(
    fake_llm: FakeScript,
) -> None:
    fake_llm.queue(prompt_reply(), prompt_reply())

    drafts = make_image_agent()(state()).update["drafts"]

    meta = drafts["instagram"]["media"][0]["meta"]
    assert meta == {
        "prompt": prompt_reply()["prompt"],
        "aspect_ratio": "4:5",
        "alt_text": "A cup of green tea on a table",
        "style_notes": "warm greens, calm mood",
        "provider": "fake",
        "size_bytes": meta["size_bytes"],
        "mime_type": "image/png",
        "width": 1024,
        "height": 1280,
    }
    assert meta["size_bytes"] > 0


def test_the_negative_prompt_is_appended_to_the_text_sent_to_the_provider(
    fake_llm: FakeScript, fake_image: FakeImage
) -> None:
    fake_llm.queue(
        prompt_reply(negative_prompt="text, logos"), prompt_reply(negative_prompt=None)
    )

    drafts = make_image_agent()(state()).update["drafts"]

    sent = [prompt for prompt, _ in fake_image.calls]
    assert sent[0].endswith("\n\nAvoid: text, logos")
    assert "Avoid" not in sent[1]
    assert drafts["facebook"]["media"][0]["meta"]["prompt"] == sent[0]


def test_the_prompt_gets_the_caption_ratios_and_strategy_of_its_platform(
    fake_llm: FakeScript,
) -> None:
    fake_llm.queue(prompt_reply(), prompt_reply())

    make_image_agent()(state(language="ur", brand_voice="playful pirate"))

    fb, ig = (c.messages[1].content for c in fake_llm.calls)
    assert "Platform: facebook" in fb and "Platform: instagram" in ig
    assert "Allowed aspect ratios: 1:1, 4:5, 16:9" in fb
    assert "Allowed aspect ratios: 1:1, 4:5\n" in ig
    assert "Facebook caption" in fb and "Instagram caption" not in fb
    assert "Output language for the alt text: ur" in ig
    assert "Brand voice: playful pirate" in ig
    assert "bright photo of tea" in ig  # strategy media notes
    assert "Tea is a daily habit" in ig  # plan


def test_a_platform_without_aspect_rules_is_told_any_ratio_is_fine(
    fake_llm: FakeScript, fake_image: FakeImage
) -> None:
    fake_llm.queue(prompt_reply(aspect_ratio="16:9"), prompt_reply(aspect_ratio="9:16"))

    make_image_agent(lambda: {})(state())

    assert "Allowed aspect ratios: any" in fake_llm.calls[0].messages[1].content
    assert [size for _, size in fake_image.calls] == [(1820, 1024), (1024, 1820)]


def test_a_ratio_the_platform_does_not_allow_is_moved_before_paying_for_the_image(
    fake_llm: FakeScript, fake_image: FakeImage
) -> None:
    fake_llm.queue(prompt_reply(aspect_ratio="16:9"), prompt_reply(aspect_ratio="16:9"))

    drafts = make_image_agent()(state()).update["drafts"]

    # Facebook allows 16:9; Instagram only 1:1 and 4:5 (nearest to 16:9: 1:1).
    assert [size for _, size in fake_image.calls] == [(1820, 1024), (1024, 1024)]
    assert drafts["instagram"]["media"][0]["meta"]["aspect_ratio"] == "1:1"


def test_the_requested_short_side_is_a_parameter(
    fake_llm: FakeScript, fake_image: FakeImage
) -> None:
    fake_llm.queue(prompt_reply(aspect_ratio="1:1"), prompt_reply(aspect_ratio="1:1"))

    make_image_agent(min_short_side=2048)(state())

    assert [size for _, size in fake_image.calls] == [(2048, 2048)] * 2


def test_the_attempt_number_is_part_of_the_storage_key(fake_llm: FakeScript) -> None:
    fake_llm.queue(prompt_reply(), prompt_reply())
    base = state(attempt_info={"image": {"attempt": 2}})

    drafts = make_image_agent()(base).update["drafts"]

    assert drafts["instagram"]["media"][0]["storage_key"].endswith("/image-3.png")


def test_a_new_attempt_never_overwrites_an_earlier_image(
    fake_llm: FakeScript, fake_image: FakeImage
) -> None:
    fake_llm.queue(*[prompt_reply()] * 4)
    agent = make_image_agent()

    first = agent(state()).update["drafts"]
    second = agent(state(attempt_info={"image": {"attempt": 1}})).update["drafts"]

    old = first["instagram"]["media"][0]["storage_key"]
    new = second["instagram"]["media"][0]["storage_key"]
    assert old != new
    assert get_storage_backend().get(old) and get_storage_backend().get(new)


def test_running_the_same_attempt_again_overwrites_its_own_object(
    fake_llm: FakeScript, media_root: Path
) -> None:
    fake_llm.queue(*[prompt_reply()] * 4)
    agent = make_image_agent()

    agent(state())
    agent(state())

    files = sorted(p.name for p in media_root.rglob("*") if p.is_file())
    assert files == ["image-1.png", "image-1.png"]  # one per platform, not four


def test_the_new_image_replaces_the_old_image_entry_only(fake_llm: FakeScript) -> None:
    fake_llm.queue(prompt_reply(), prompt_reply())
    base = state()
    base["drafts"]["instagram"]["media"] = [  # type: ignore[index]
        {"type": "image", "storage_key": "old"},
        {"type": "audio", "storage_key": "keep"},
        "not a dict",
    ]

    media = make_image_agent()(base).update["drafts"]["instagram"]["media"]

    assert media[:2] == [{"type": "audio", "storage_key": "keep"}, "not a dict"]
    assert media[2]["type"] == "image"


def test_an_image_that_is_not_an_image_is_stored_but_has_no_size(
    fake_llm: FakeScript, fake_image: FakeImage
) -> None:
    fake_image.queue(b"not an image", b"also not")
    fake_llm.queue(prompt_reply(), prompt_reply())

    drafts = make_image_agent()(state()).update["drafts"]

    media = drafts["facebook"]["media"][0]
    assert media["storage_key"] == "requests/r1/facebook/image-1.bin"
    assert get_storage_backend().get(media["storage_key"]) == b"not an image"
    assert "width" not in media["meta"] and "mime_type" not in media["meta"]


@pytest.mark.parametrize("caption", [None, ""])
def test_a_missing_caption_is_a_bug_not_a_retry(
    fake_llm: FakeScript, caption: str | None
) -> None:
    base = state()
    base["drafts"]["facebook"]["caption"] = caption  # type: ignore[index]

    with pytest.raises(ValueError, match="facebook caption"):
        make_image_agent()(base)
    assert fake_llm.calls == []


@pytest.mark.parametrize("request_id", [None, ""])
def test_a_missing_request_id_is_a_bug(fake_llm: FakeScript, request_id: Any) -> None:
    base = state(request_id=request_id)

    with pytest.raises(ValueError, match="request_id"):
        make_image_agent()(base)


def test_a_missing_strategy_is_a_bug_not_a_retry(fake_llm: FakeScript) -> None:
    base = state(strategy={"instagram": strat("instagram")})
    fake_llm.queue(prompt_reply())

    with pytest.raises(ValueError, match="facebook"):
        make_image_agent()(base)


def test_provider_errors_propagate_for_the_checked_node_to_handle(
    fake_llm: FakeScript, fake_image: FakeImage
) -> None:
    fake_image.fail_next(1)
    fake_llm.queue(prompt_reply(), prompt_reply())

    with pytest.raises(ImageProviderError):
        make_image_agent()(state())


def test_an_invalid_reply_raises_an_output_error(fake_llm: FakeScript) -> None:
    fake_llm.queue({"prompt": ""}, "nonsense")

    with pytest.raises(LLMOutputError):
        make_image_agent()(state())


def test_a_storage_configuration_error_propagates(
    fake_llm: FakeScript, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("STORAGE_BACKEND", "nowhere")
    get_settings.cache_clear()

    with pytest.raises(StorageConfigError):
        make_image_agent()(state())


def test_retry_feedback_reaches_every_platform_prompt(fake_llm: FakeScript) -> None:
    fake_llm.queue(prompt_reply(), prompt_reply())

    make_image_agent()(state(feedback={"image": "Make it simpler."}))

    for call in fake_llm.calls:
        assert "Make it simpler." in call.messages[1].content


# --------------------------------------------------------------------------
# Rule checks (they read the stored bytes)
# --------------------------------------------------------------------------


def test_good_images_pass() -> None:
    assert make_check_image()(imaged()).ok is True


def test_no_media_fails() -> None:
    result = make_check_image()(state())

    assert codes(result) == ["image_missing", "image_missing"]
    assert "instagram: create exactly one image, found 0." in result.feedback


def test_two_images_fail() -> None:
    base = imaged()
    media = base["drafts"]["instagram"]["media"]  # type: ignore[index]
    media.append(media[0])

    result = make_check_image()(base)

    assert codes(result) == ["image_missing"]
    assert "found 2" in result.feedback


def test_media_that_is_not_an_image_entry_does_not_count() -> None:
    base = imaged()
    base["drafts"]["facebook"]["media"] = [{"type": "audio"}, "x"]  # type: ignore[index]

    assert codes(make_check_image()(base)) == ["image_missing"]


def test_media_that_is_not_a_list_fails() -> None:
    base = imaged()
    base["drafts"]["facebook"]["media"] = "image.png"  # type: ignore[index]

    assert codes(make_check_image()(base)) == ["image_missing"]


@pytest.mark.parametrize("key", [None, 5, "requests/r1/instagram/never-stored.png"])
def test_an_image_that_is_not_in_storage_fails(key: Any) -> None:
    base = imaged()
    base["drafts"]["instagram"]["media"][0]["storage_key"] = key  # type: ignore[index]

    result = make_check_image()(base)

    assert codes(result) == ["image_missing"]
    assert "was not found" in result.feedback


def test_bytes_that_are_not_an_image_fail() -> None:
    result = make_check_image()(imaged(instagram=b"<html>not an image</html>"))

    assert codes(result) == ["image_invalid"]
    assert "not a valid PNG, JPEG or WebP" in result.feedback


@pytest.mark.parametrize(
    ("size", "ok"),
    [(DEFAULT_MAX_IMAGE_BYTES, True), (DEFAULT_MAX_IMAGE_BYTES + 1, False)],
)
def test_the_size_limit_is_a_parameter_with_an_8mb_default(size: int, ok: bool) -> None:
    png = build_png(1024, 1280, (1, 2, 3))
    check = make_check_image(max_image_bytes=len(png) if ok else len(png) - 1)

    result = check(imaged(instagram=png))

    assert result.ok is ok
    if not ok:
        assert codes(result) == ["image_too_large"]
    assert size > 0


@pytest.mark.parametrize(
    ("width", "height", "ok"),
    [(1024, 1280, True), (819, 1024, False), (1024, 1280, True), (1000, 1250, False)],
)
def test_the_short_side_must_reach_the_minimum(
    width: int, height: int, ok: bool
) -> None:
    result = make_check_image()(imaged(instagram=build_png(width, height, (1, 2, 3))))

    assert result.ok is ok
    if not ok:
        assert codes(result) == ["image_too_small"]
        assert f"{width}x{height}" in result.feedback


def test_the_minimum_short_side_is_a_parameter() -> None:
    small = build_png(512, 640, (1, 2, 3))

    assert make_check_image(min_short_side=512)(imaged(instagram=small)).ok is True
    assert make_check_image()(imaged(instagram=small)).ok is False


@pytest.mark.parametrize(
    ("width", "height", "ok"),
    [
        (1024, 1280, True),  # exactly 4:5
        (1024, 1024, True),  # 1:1 is allowed too
        (1100, 1353, True),  # 1.6% off 4:5 is within the tolerance
        (1024, 1280 + 60, False),  # about 4.7% off
        (1792, 1024, False),  # 16:9 is not allowed on Instagram
        (1280, 1024, False),
    ],
)
def test_the_real_aspect_ratio_must_be_allowed(
    width: int, height: int, ok: bool
) -> None:
    result = make_check_image()(imaged(instagram=build_png(width, height, (1, 2, 3))))

    assert result.ok is ok
    if not ok:
        assert codes(result) == ["image_aspect_not_allowed"]
        assert "1:1, 4:5" in result.feedback


def test_a_platform_without_aspect_rules_accepts_any_ratio() -> None:
    wide = build_png(2048, 1024, (1, 2, 3))

    assert make_check_image(lambda: {})(imaged(instagram=wide, facebook=wide)).ok


def test_an_unusable_allowed_ratio_in_the_rules_is_ignored_by_the_check() -> None:
    wide = build_png(2048, 1024, (1, 2, 3))
    rules = {"instagram": {"image_aspect": ["bad"]}, "facebook": {}}

    result = make_check_image(lambda: rules)(imaged(instagram=wide))

    assert codes(result) == ["image_aspect_not_allowed"]


@pytest.mark.parametrize("alt", [None, "", "   ", 5])
def test_the_alt_text_is_required(alt: Any) -> None:
    base = imaged()
    base["drafts"]["instagram"]["media"][0]["meta"]["alt_text"] = alt  # type: ignore[index]

    result = make_check_image()(base)

    assert codes(result) == ["alt_text_missing"]


@pytest.mark.parametrize(("size", "ok"), [(200, True), (201, False)])
def test_the_alt_text_has_at_most_200_characters(size: int, ok: bool) -> None:
    base = imaged()
    base["drafts"]["instagram"]["media"][0]["meta"]["alt_text"] = "a" * size  # type: ignore[index]

    result = make_check_image()(base)

    assert result.ok is ok
    if not ok:
        assert codes(result) == ["alt_text_too_long"]


def test_media_without_meta_fails_on_the_alt_text() -> None:
    base = imaged()
    del base["drafts"]["facebook"]["media"][0]["meta"]  # type: ignore[index]

    assert codes(make_check_image()(base)) == ["alt_text_missing"]


def test_every_problem_is_reported_in_one_feedback() -> None:
    result = make_check_image()(
        imaged(instagram=build_png(800, 400, (1, 2, 3)), facebook=b"junk")
    )

    assert set(codes(result)) == {
        "image_too_small",
        "image_aspect_not_allowed",
        "image_invalid",
    }
    assert "instagram" in result.feedback and "facebook" in result.feedback


def test_the_rules_are_read_on_every_run() -> None:
    rules = {"instagram": {"image_aspect": ["4:5"]}, "facebook": {}}
    check = make_check_image(lambda: rules)
    base = imaged()

    assert check(base).ok is True
    rules["instagram"]["image_aspect"] = ["16:9"]
    assert codes(check(base)) == ["image_aspect_not_allowed"]


def test_the_rules_do_not_call_the_llm_or_the_image_provider(
    fake_llm: FakeScript, fake_image: FakeImage
) -> None:
    make_check_image()(imaged())

    assert fake_llm.calls == [] and fake_image.calls == []


# --------------------------------------------------------------------------
# Judge (the text-only variant: image prompt against the caption)
# --------------------------------------------------------------------------


def test_the_judge_sends_prompt_alt_text_and_caption_to_the_fast_model(
    fake_llm: FakeScript,
) -> None:
    fake_llm.queue(good_judge())
    base = imaged()
    meta = base["drafts"]["instagram"]["media"][0]["meta"]  # type: ignore[index]
    meta.update(prompt="A cup of tea at dawn", style_notes="calm greens")

    result = make_image_judge()(base)

    assert result.ok is True
    (call,) = fake_llm.calls
    assert call.kind == "fast" and call.temperature == 0.0
    user = call.messages[1].content
    assert "Step being judged: image" in user
    assert IMAGE_RUBRIC.splitlines()[0] in user
    assert "A cup of tea at dawn" in user and "calm greens" in user
    assert "Instagram caption" in user and "Facebook caption" in user
    assert "Brand voice: warm and simple" in user


def test_the_judge_copes_with_a_draft_without_an_image(fake_llm: FakeScript) -> None:
    fake_llm.queue(good_judge())

    make_image_judge()(state())

    assert "Instagram caption" in fake_llm.calls[0].messages[1].content


# --------------------------------------------------------------------------
# The checked node (agent + rules + judge + retry + escalation)
# --------------------------------------------------------------------------


class Flow:
    """START -> image (checked) -> END."""

    def __init__(self, **node_args: Any) -> None:
        self.steps: list[StepRecord] = []
        self.node = make_image_node(**node_args)
        builder = StateGraph(GraphState)
        entry = add_checked_node(builder, self.node, on_pass=END)
        builder.add_edge(START, entry)
        self.graph = builder.compile(checkpointer=InMemorySaver())
        self.config = {
            "configurable": {
                "thread_id": str(uuid.uuid4()),
                "step_recorder": self.steps.append,
            }
        }

    def run(self, initial: GraphState | None = None) -> dict[str, Any]:
        return self.graph.invoke(initial or state(), self.config)

    def resume(self, value: Any) -> dict[str, Any]:
        return self.graph.invoke(Command(resume=value), self.config)


def test_good_images_pass_at_once(fake_llm: FakeScript, fake_image: FakeImage) -> None:
    fake_llm.queue(prompt_reply(), prompt_reply(), good_judge())
    flow = Flow()

    result = flow.run()

    assert all(
        len(result["drafts"][p]["media"]) == 1 for p in ("facebook", "instagram")
    )
    assert [c.kind for c in fake_llm.calls] == ["text", "text", "fast"]
    (step,) = flow.steps
    assert (step.node_name, step.attempt, step.verdict) == ("image", 1, "passed")
    assert step.output_summary["prompt_version"] == "image.v1"
    assert step.output_summary["score"] == 0.9


def test_a_bad_image_retries_with_feedback_that_changes_the_second_attempt(
    fake_llm: FakeScript, fake_image: FakeImage
) -> None:
    small = build_png(512, 640, (1, 2, 3))
    fake_image.queue(small, small)  # attempt 1: both too small; attempt 2: the fake
    fake_llm.queue(
        prompt_reply(prompt="first idea", aspect_ratio="1:1"),
        prompt_reply(prompt="first idea", aspect_ratio="4:5"),
        prompt_reply(prompt="second idea", aspect_ratio="1:1"),
        prompt_reply(prompt="second idea", aspect_ratio="4:5"),
        good_judge(),
    )
    flow = Flow()

    result = flow.run()

    first, second = fake_llm.calls[0], fake_llm.calls[2]
    assert "Previous attempt feedback" not in first.messages[1].content
    assert "Previous attempt feedback" in second.messages[1].content
    assert "the short side must be at least 1024 pixels" in second.messages[1].content
    sent = [prompt for prompt, _ in fake_image.calls]
    assert sent == ["first idea", "first idea", "second idea", "second idea"]
    instagram = result["drafts"]["instagram"]["media"][0]
    assert instagram["storage_key"].endswith("/image-2.png")  # attempt 2's object
    assert instagram["meta"]["prompt"] == "second idea"
    assert [c.kind for c in fake_llm.calls][-1] == "fast"  # the judge ran once
    assert [s.verdict for s in flow.steps] == ["retried", "passed"]
    assert result["retries"] == {"image": 1}


def test_a_low_judge_score_retries_with_the_judge_feedback(
    fake_llm: FakeScript,
) -> None:
    low = {
        "ok": False,
        "score": 0.4,
        "issues": [
            {"code": "off_topic", "message": "Match the caption.", "severity": "error"}
        ],
        "feedback": "Match the caption. Keep: the style",
    }
    fake_llm.queue(
        prompt_reply(),
        prompt_reply(),
        low,
        prompt_reply(),
        prompt_reply(),
        good_judge(),
    )
    flow = Flow()

    flow.run()

    assert "Match the caption. Keep: the style" in fake_llm.calls[3].messages[1].content
    assert [s.verdict for s in flow.steps] == ["retried", "passed"]


def test_exhausted_retries_escalate_and_resume_retry_continues(
    fake_llm: FakeScript, fake_image: FakeImage
) -> None:
    junk = b"not an image"
    fake_image.queue(*[junk] * 6)
    fake_llm.queue(*[prompt_reply()] * 8, good_judge())
    flow = Flow()

    paused = flow.run()

    (interrupt,) = paused["__interrupt__"]
    assert interrupt.value["step"] == "image"
    assert interrupt.value["reason"] == "retries_exhausted"
    assert {i["code"] for i in interrupt.value["issues"]} == {"image_invalid"}

    done = flow.resume({"action": "retry"})

    assert done["drafts"]["instagram"]["media"][0]["storage_key"].endswith(".png")


def test_a_retriable_provider_outage_is_a_retried_error_attempt(
    fake_llm: FakeScript, fake_image: FakeImage
) -> None:
    fake_image.fail_next(1)
    fake_llm.queue(*[prompt_reply()] * 4, good_judge())
    flow = Flow()

    result = flow.run()

    assert len(result["drafts"]["facebook"]["media"]) == 1
    assert [s.verdict for s in flow.steps] == ["error", "passed"]


def test_a_provider_configuration_error_escalates_at_once(
    fake_llm: FakeScript, fake_image: FakeImage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        FakeImage,
        "_generate",
        lambda self, prompt, size: (_ for _ in ()).throw(ImageConfigError()),
    )
    fake_llm.queue(prompt_reply(), prompt_reply())
    flow = Flow()

    paused = flow.run()

    (interrupt,) = paused["__interrupt__"]
    assert interrupt.value["reason"] == "non_retriable_error"
    assert interrupt.value["error_code"] == "image_not_configured"
    assert [s.verdict for s in flow.steps] == ["escalated"]


def test_without_the_judge_only_the_rules_decide(fake_llm: FakeScript) -> None:
    fake_llm.queue(prompt_reply(), prompt_reply())

    Flow(use_judge=False).run()

    assert [c.kind for c in fake_llm.calls] == ["text", "text"]


def test_the_limits_are_node_parameters(
    fake_llm: FakeScript, fake_image: FakeImage
) -> None:
    fake_llm.queue(*[prompt_reply()] * 2)
    flow = Flow(max_retries=1, use_judge=False, max_image_bytes=10)

    paused = flow.run()

    assert paused["__interrupt__"][0].value["max_retries"] == 1
    assert {i["code"] for i in paused["__interrupt__"][0].value["issues"]} == {
        "image_too_large"
    }


def test_the_node_uses_the_documented_step_key_and_default_limits() -> None:
    node = make_image_node()

    assert node.key == "image"
    assert node.max_retries == 3
