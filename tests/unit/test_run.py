"""Tests for the dev runner ``python -m app.agents.run`` (B2-12), fully offline."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from app.agents.fake_flow import GENERIC_TAGS, post_flow_replies
from app.agents.llm import FakeCall, FakeScript, Message
from app.agents.run import (
    EXIT_FAILED,
    EXIT_OK,
    EXIT_PAUSED,
    build_state,
    main,
    parse_args,
    run,
    text_report,
)
from app.providers.image import read_image_info
from app.storage import get_storage_backend

pytestmark = pytest.mark.usefixtures("media_root", "fake_image", "fake_llm")

ROOT = Path(__file__).resolve().parents[2]


def args(*extra: str) -> list[str]:
    return ["--topic", "Benefits of green tea", *extra]


# --------------------------------------------------------------------------
# Arguments and the request state
# --------------------------------------------------------------------------


def test_defaults() -> None:
    parsed = parse_args(args())

    assert parsed.content_type == "post"
    assert parsed.platforms == "facebook,instagram"
    assert parsed.language == "en" and parsed.brand_voice is None
    assert parsed.max_review_cycles == 2 and parsed.json is False


def test_a_topic_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        parse_args([])

    assert exit_info.value.code == 2
    assert "--topic" in capsys.readouterr().err


def test_video_is_not_selectable_yet(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        parse_args(args("--type", "video"))


def test_the_state_has_one_account_per_platform_and_a_future_publish_time() -> None:
    state = build_state(
        parse_args(
            args("--platforms", " Facebook , instagram,,", "--brand-voice", "playful")
        )
    )

    assert state["target_accounts"] == [
        {"account_id": "dev-facebook", "platform": "facebook"},
        {"account_id": "dev-instagram", "platform": "instagram"},
    ]
    assert state["brand_voice"] == "playful" and state["language"] == "en"
    assert state["publish_at"].endswith("+00:00")
    assert state["content_type"] == "post" and state["topic"] == "Benefits of green tea"
    assert len(state["request_id"]) == 36


def test_every_run_gets_a_new_request_id_and_no_brand_voice_by_default() -> None:
    first = build_state(parse_args(args()))
    second = build_state(parse_args(args()))

    assert first["request_id"] != second["request_id"]
    assert "brand_voice" not in first


# --------------------------------------------------------------------------
# A whole run
# --------------------------------------------------------------------------


def test_a_run_prints_a_draft_per_platform_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(args())

    out = capsys.readouterr()
    assert code == EXIT_OK
    for platform in ("facebook", "instagram"):
        assert f"=== {platform} ===" in out.out
    assert "Hashtags: #benefits #green #tea" in out.out
    assert "Image: requests/" in out.out and "(1024x1024, image/png)" in out.out
    assert "Alt text: Illustration for Benefits of green tea" in out.out
    assert "=== review: ok (fix cycles: 0) ===" in out.out
    for step in (
        "planner",
        "researcher",
        "strategist",
        "writer",
        "image",
        "hashtag",
        "reviewer",
    ):
        assert f"step {step} attempt 1: passed" in out.err


def test_the_stored_images_are_real_images() -> None:
    code, report = run(parse_args(args()))

    assert code == EXIT_OK
    for draft in report["drafts"].values():
        key = draft["media"][0]["storage_key"]
        info = read_image_info(get_storage_backend().get(key))
        assert (info.width, info.height) == (1024, 1024)  # type: ignore[union-attr]
    assert report["status"] != "failed" and report["errors"] == []


def test_json_output_is_parseable_and_complete(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(args("--json", "--platforms", "instagram"))

    report = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK
    assert set(report["drafts"]) == {"instagram"}
    assert report["review"]["ok"] is True and report["review_cycles"] == 0
    assert report["drafts"]["instagram"]["hashtags"][0] == "#benefits"


def test_the_caption_is_whole_sentences_near_the_length_target() -> None:
    _, report = run(parse_args(args()))

    caption = report["drafts"]["facebook"]["caption"]
    assert 420 <= len(caption) <= 780 and caption.endswith(".")


def test_the_language_and_brand_voice_reach_the_prompts(fake_llm: FakeScript) -> None:
    run(parse_args(args("--language", "ur", "--brand-voice", "playful pirate")))

    users = " ".join(c.messages[-1].content for c in fake_llm.calls)
    assert "Output language: ur" in users and "Brand voice: playful pirate" in users


def test_a_rejected_request_exits_one_with_the_reasons(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(args("--platforms", "tiktok"))

    out = capsys.readouterr().out
    assert code == EXIT_FAILED
    assert "Request rejected or failed:" in out
    assert "tiktok does not support post content." in out


def test_a_rejected_request_in_json_mode_lists_the_errors(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(args("--platforms", "tiktok", "--json"))

    report = json.loads(capsys.readouterr().out)
    assert code == EXIT_FAILED and report["status"] == "failed"
    assert report["errors"] == ["tiktok does not support post content."]


def test_a_run_that_needs_a_human_exits_two_and_prints_the_interrupt(
    capsys: pytest.CaptureFixture[str], fake_llm: FakeScript
) -> None:
    fake_llm.queue(*["not json"] * 8)  # the planner can never produce a plan

    code = main(args())

    out = capsys.readouterr().out
    assert code == EXIT_PAUSED
    assert "Paused for a human:" in out
    assert '"step": "planner"' in out and '"reason": "retries_exhausted"' in out


def test_a_paused_run_in_json_mode_has_the_payload(
    capsys: pytest.CaptureFixture[str], fake_llm: FakeScript
) -> None:
    fake_llm.queue(*["not json"] * 8)

    code = main(args("--json"))

    report = json.loads(capsys.readouterr().out)
    assert code == EXIT_PAUSED and report["paused"]["step"] == "planner"
    assert "drafts" not in report


def test_with_no_fix_cycles_a_review_error_becomes_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_args = parse_args(args("--max-review-cycles", "0"))
    original = post_flow_replies(run_args.topic, "en", ["facebook", "instagram"])

    def responder(call: FakeCall) -> Any:
        reply = original(call)
        if "final reviewer" in call.messages[0].content:
            reply = {
                **reply,
                "ok": False,
                "issues": [
                    {
                        "severity": "error",
                        "platform": "facebook",
                        "responsible_node": "writer",
                        "message": "Weak hook.",
                        "fix_hint": "Open stronger.",
                    }
                ],
            }
        return reply

    monkeypatch.setattr("app.agents.run.post_flow_replies", lambda *a, **k: responder)

    code, report = run(run_args)

    assert code == EXIT_OK
    assert report["review"]["warnings"] == ["facebook: Weak hook."]
    assert report["review_cycles"] == 0


def test_the_storage_dir_option_sets_where_images_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path / "old"))
    target = tmp_path / "custom"

    code, _ = run(parse_args(args("--storage-dir", str(target))))

    assert code == EXIT_OK
    assert list(target.rglob("image-1.png"))
    assert not (tmp_path / "old").exists()


def test_a_real_provider_is_not_scripted(
    monkeypatch: pytest.MonkeyPatch, fake_llm: FakeScript
) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("LLM_PROVIDER", "nowhere")
    get_settings.cache_clear()

    code, report = run(parse_args(args()))

    assert fake_llm._default is None  # the runner left the fake script alone
    assert code == EXIT_PAUSED  # the unknown provider escalates the first step
    assert report["paused"]["step"] == "planner"


def test_the_module_runs_as_a_command_with_no_network(tmp_path: Path) -> None:
    env = {
        **os.environ,
        "LLM_PROVIDER": "fake",
        "SEARCH_PROVIDER": "fake",
        "IMAGE_PROVIDER": "fake",
        "STORAGE_BACKEND": "local",
        "STORAGE_PATH": str(tmp_path / "media"),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.agents.run",
            "--topic",
            "Benefits of green tea",
            "--type",
            "post",
            "--platforms",
            "facebook,instagram",
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={**env, "PYTHONPATH": str(ROOT / "backend")},
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert "=== facebook ===" in result.stdout and "=== instagram ===" in result.stdout


# --------------------------------------------------------------------------
# The scripted fake replies
# --------------------------------------------------------------------------


def call(system: str, user: str = "") -> FakeCall:
    return FakeCall(
        messages=[Message("system", system), Message("user", user)],
        kind="text",
        temperature=0.0,
        model="fake-text",
        json_schema=None,
    )


def test_an_unknown_prompt_is_refused() -> None:
    responder = post_flow_replies("A topic", "en", ["instagram"])

    with pytest.raises(ValueError, match="unknown prompt role"):
        responder(call("You are a pirate."))


def test_hashtags_follow_the_requested_count_and_never_repeat() -> None:
    responder = post_flow_replies("Tea tea TEA 2026 and café", "en", ["instagram"])

    tags = responder(call("hashtag specialist", "Number of hashtags wanted: 8"))[
        "hashtags"
    ]

    assert len(tags) == 8 and len({t.casefold() for t in tags}) == 8
    assert tags[:3] == ["#tea", "#and", "#café"]  # words once, numbers skipped
    assert all(t.startswith("#") for t in tags)


def test_hashtags_default_to_five_without_a_count() -> None:
    responder = post_flow_replies("A topic", "en", ["instagram"])

    assert len(responder(call("hashtag specialist", "nothing"))["hashtags"]) == 5


def test_hashtags_are_padded_with_generic_ones() -> None:
    responder = post_flow_replies("Hi", "en", ["instagram"])

    tags = responder(call("hashtag specialist", "Number of hashtags wanted: 15"))[
        "hashtags"
    ]

    assert len(tags) == 15 and "#" + GENERIC_TAGS[0] in tags


def test_the_strategy_respects_each_platforms_hashtag_range() -> None:
    responder = post_flow_replies("A topic", "en", ["facebook", "instagram"])

    reply = responder(call("social media strategist"))

    assert reply["facebook"]["hashtag_count"] == 5  # facebook allows 0 to 5
    assert reply["instagram"]["hashtag_count"] == 5  # instagram allows 3 to 15
    assert reply["instagram"]["video"] is None


# --------------------------------------------------------------------------
# The text report
# --------------------------------------------------------------------------


def test_the_report_shows_a_title_issues_and_warnings() -> None:
    report = text_report(
        {
            "drafts": {
                "youtube": {
                    "title": "Green tea in 3 steps",
                    "caption": "Body text.",
                    "hashtags": [],
                    "media": [],
                }
            },
            "review": {
                "ok": False,
                "issues": [
                    {
                        "severity": "error",
                        "responsible_node": "writer",
                        "platform": None,
                        "message": "Weak hook.",
                    }
                ],
                "warnings": ["Check the date."],
            },
            "review_cycles": 2,
        }
    )

    assert report.splitlines()[:3] == [
        "=== youtube ===",
        "Title: Green tea in 3 steps",
        "Body text.",
    ]
    assert "Hashtags: (none)" in report
    assert "=== review: NOT ok (fix cycles: 2) ===" in report
    assert "- error [writer] all: Weak hook." in report
    assert "- warning: Check the date." in report


def test_a_draft_without_text_and_no_review_is_reported_plainly() -> None:
    report = text_report({"drafts": {"instagram": {}}})

    assert "(no caption)" in report and "review" not in report
    assert text_report({}) == ""
