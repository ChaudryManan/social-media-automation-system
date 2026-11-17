"""Tests for ``app.agents.llm`` (B2-01). No network: everything uses the fake."""

import logging
import threading
from typing import Literal

import pytest
from app.agents import llm
from app.agents.llm import (
    ChatModel,
    FakeCall,
    FakeChatModel,
    FakeScript,
    FakeScriptExhaustedError,
    LLMCompletion,
    LLMConfigError,
    LLMOutputError,
    LLMProviderError,
    Message,
    capture_usage,
    extract_json,
    generate_structured,
    get_chat_model,
    register_provider,
    user_message,
)
from app.core.config import Settings
from pydantic import BaseModel, Field


class Plan(BaseModel):
    angle: str
    key_messages: list[str] = Field(min_length=2, max_length=5)
    goal: Literal["awareness", "engagement"]
    language: str = "en"


GOOD = {
    "angle": "Consistency beats intensity",
    "key_messages": ["Start small", "Track weekly"],
    "goal": "awareness",
    "language": "en",
}
PROMPT = [Message("system", "You are a planner."), user_message("Topic: habits")]


def settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Message
# --------------------------------------------------------------------------


def test_message_helpers_set_the_role() -> None:
    assert llm.system_message("a") == Message("system", "a")
    assert llm.user_message("b") == Message("user", "b")
    assert llm.assistant_message("c") == Message("assistant", "c")


def test_message_rejects_unknown_role() -> None:
    with pytest.raises(ValueError):
        Message("tool", "x")  # type: ignore[arg-type]


def test_message_rejects_non_string_content() -> None:
    with pytest.raises(TypeError):
        Message("user", 3)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Fake provider scripting
# --------------------------------------------------------------------------


def test_fixture_forces_fake_even_if_env_selects_gemini(
    fake_llm: FakeScript, monkeypatch: pytest.MonkeyPatch
) -> None:
    # fixture already ran; the model must be the fake one regardless of .env
    assert get_chat_model().provider == "fake"


def test_queue_returns_responses_in_order(fake_llm: FakeScript) -> None:
    fake_llm.queue("first", "second")
    model = get_chat_model()
    assert model.invoke(PROMPT).text == "first"
    assert model.invoke(PROMPT).text == "second"
    assert fake_llm.pending == 0


def test_queue_serialises_dicts_lists_and_models(fake_llm: FakeScript) -> None:
    fake_llm.queue({"a": 1}, [1, 2], Plan(**GOOD))
    model = get_chat_model()
    assert model.invoke(PROMPT).text == '{"a": 1}'
    assert model.invoke(PROMPT).text == "[1, 2]"
    assert Plan.model_validate_json(model.invoke(PROMPT).text) == Plan(**GOOD)


def test_empty_script_fails_loudly(fake_llm: FakeScript) -> None:
    with pytest.raises(FakeScriptExhaustedError) as err:
        get_chat_model().invoke(PROMPT)
    assert err.value.code == "fake_llm_script_exhausted"


def test_default_response_used_when_queue_is_empty(fake_llm: FakeScript) -> None:
    fake_llm.queue("queued").set_default("fallback")
    model = get_chat_model()
    assert [model.invoke(PROMPT).text for _ in range(3)] == [
        "queued",
        "fallback",
        "fallback",
    ]


def test_default_can_be_a_function_of_the_call(fake_llm: FakeScript) -> None:
    fake_llm.set_default(lambda call: {"echo": call.messages[-1].content})
    reply = get_chat_model().invoke([user_message("hello")]).text
    assert reply == '{"echo": "hello"}'


def test_fail_next_fails_exactly_n_times_then_recovers(fake_llm: FakeScript) -> None:
    fake_llm.fail_next(2).queue("ok")
    model = get_chat_model()
    for _ in range(2):
        with pytest.raises(LLMProviderError) as err:
            model.invoke(PROMPT)
        assert err.value.retriable is True
        assert err.value.code == "llm_provider_error"
    assert model.invoke(PROMPT).text == "ok"


def test_fail_next_accepts_a_custom_error(fake_llm: FakeScript) -> None:
    boom = LLMProviderError("quota", retriable=False)
    fake_llm.fail_next(1, error=boom)
    with pytest.raises(LLMProviderError) as err:
        get_chat_model().invoke(PROMPT)
    assert err.value is boom
    assert err.value.retriable is False


def test_exception_in_queue_is_raised_in_order(fake_llm: FakeScript) -> None:
    fake_llm.queue("ok", LLMProviderError("second call fails"), "ok again")
    model = get_chat_model()
    assert model.invoke(PROMPT).text == "ok"
    with pytest.raises(LLMProviderError):
        model.invoke(PROMPT)
    assert model.invoke(PROMPT).text == "ok again"


def test_unexpected_provider_exception_is_wrapped(fake_llm: FakeScript) -> None:
    fake_llm.queue(RuntimeError("secret internal detail"))
    with pytest.raises(LLMProviderError) as err:
        get_chat_model().invoke(PROMPT)
    assert "secret" not in err.value.message
    assert err.value.details == {"error_type": "RuntimeError"}
    assert isinstance(err.value.__cause__, RuntimeError)


def test_calls_are_recorded_with_prompt_kind_model_and_temperature(
    fake_llm: FakeScript,
) -> None:
    fake_llm.queue("a", "b")
    get_chat_model("text").invoke(PROMPT, temperature=0.8)
    get_chat_model("fast").invoke([user_message("second")])
    first, second = fake_llm.calls
    assert isinstance(first, FakeCall)
    assert first.kind == "text" and first.model == "fake-text"
    assert first.temperature == 0.8
    assert first.messages == tuple(PROMPT)
    assert "Topic: habits" in first.prompt_text
    assert second.kind == "fast" and second.model == "fake-fast"
    assert second.temperature is None


def test_failed_calls_are_recorded_too(fake_llm: FakeScript) -> None:
    fake_llm.fail_next(1)
    with pytest.raises(LLMProviderError):
        get_chat_model().invoke(PROMPT)
    assert len(fake_llm.calls) == 1


def test_reset_clears_everything(fake_llm: FakeScript) -> None:
    fake_llm.queue("x").fail_next(3).set_default("d")
    fake_llm.reset()
    assert fake_llm.pending == 0 and fake_llm.calls == []
    with pytest.raises(FakeScriptExhaustedError):
        get_chat_model().invoke(PROMPT)


def test_scripts_can_be_isolated_from_the_shared_one(fake_llm: FakeScript) -> None:
    own = FakeScript().queue("mine")
    model = FakeChatModel(model="m", kind="text", script=own)
    assert model.invoke(PROMPT).text == "mine"
    assert len(own.calls) == 1
    assert fake_llm.calls == []  # the shared script was not touched


def test_fake_script_is_thread_safe() -> None:
    script = FakeScript().queue(*[str(i) for i in range(50)])
    model = FakeChatModel(model="m", kind="text", script=script)
    seen: list[str] = []

    def work() -> None:
        for _ in range(10):
            seen.append(model.invoke(PROMPT).text)

    threads = [threading.Thread(target=work) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(seen, key=int) == [str(i) for i in range(50)]


def test_fail_next_rejects_negative_counts() -> None:
    with pytest.raises(ValueError):
        FakeScript().fail_next(-1)


def test_reset_fake_script_clears_the_shared_script(fake_llm: FakeScript) -> None:
    fake_llm.queue("x")
    llm.reset_fake_script()
    assert llm.get_fake_script() is fake_llm
    assert fake_llm.pending == 0


def test_invoke_rejects_empty_messages(fake_llm: FakeScript) -> None:
    with pytest.raises(ValueError):
        get_chat_model().invoke([])


# --------------------------------------------------------------------------
# Provider / model selection from settings
# --------------------------------------------------------------------------


def test_model_names_come_from_settings() -> None:
    cfg = settings(
        LLM_PROVIDER="fake", LLM_MODEL_TEXT="big-1", LLM_MODEL_FAST="small-1"
    )
    assert get_chat_model("text", settings=cfg).model == "big-1"
    assert get_chat_model("fast", settings=cfg).model == "small-1"


def test_fast_falls_back_to_text_model() -> None:
    cfg = settings(LLM_PROVIDER="fake", LLM_MODEL_TEXT="big-1")
    assert get_chat_model("fast", settings=cfg).model == "big-1"


def test_blank_model_names_count_as_unset() -> None:
    cfg = settings(LLM_PROVIDER="fake", LLM_MODEL_TEXT="  ", LLM_MODEL_FAST="")
    assert get_chat_model("text", settings=cfg).model == "fake-text"
    assert get_chat_model("fast", settings=cfg).model == "fake-fast"


def test_provider_name_is_case_and_space_insensitive() -> None:
    assert get_chat_model(settings=settings(LLM_PROVIDER=" FAKE ")).provider == "fake"


def test_unavailable_provider_is_a_clear_config_error() -> None:
    with pytest.raises(LLMConfigError) as err:
        get_chat_model(settings=settings(LLM_PROVIDER="gemini"))
    assert err.value.code == "llm_not_configured"
    assert err.value.details == {"available": ["fake"]}


def test_unknown_kind_is_rejected() -> None:
    with pytest.raises(LLMConfigError):
        get_chat_model("huge", settings=settings(LLM_PROVIDER="fake"))  # type: ignore[arg-type]


def test_registered_provider_is_used_and_must_get_its_model_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(llm, "_PROVIDERS", dict(llm._PROVIDERS))

    class Echo(ChatModel):
        provider = "echo"

        def _generate(self, messages, *, temperature, json_schema):  # type: ignore[no-untyped-def]
            return LLMCompletion(text=messages[-1].content, tokens_in=1, tokens_out=1)

    def factory(model, kind, cfg):  # type: ignore[no-untyped-def]
        return Echo(model=llm.require_model_name(model, kind), kind=kind)

    register_provider("Echo", factory)
    cfg = settings(LLM_PROVIDER="echo", LLM_MODEL_TEXT="echo-1")
    model = get_chat_model(settings=cfg)
    assert model.provider == "echo" and model.model == "echo-1"
    assert model.invoke([user_message("hi")]).text == "hi"

    with pytest.raises(LLMConfigError) as err:
        get_chat_model(settings=settings(LLM_PROVIDER="echo"))
    assert "LLM_MODEL_TEXT" in err.value.message


# --------------------------------------------------------------------------
# extract_json
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1}',
        '  {"a": 1}\n',
        '```json\n{"a": 1}\n```',
        '```\n{"a": 1}\n```',
        'Sure! Here is the JSON: {"a": 1} Hope that helps.',
    ],
)
def test_extract_json_accepts_common_model_formats(text: str) -> None:
    assert extract_json(text) == {"a": 1}


def test_extract_json_accepts_arrays() -> None:
    assert extract_json("Result: [1, 2, 3]") == [1, 2, 3]


@pytest.mark.parametrize("text", ["", "   ", "no json here", '{"a": ', "{{{{{{{{"])
def test_extract_json_rejects_unusable_text(text: str) -> None:
    with pytest.raises(ValueError):
        extract_json(text)


# --------------------------------------------------------------------------
# generate_structured
# --------------------------------------------------------------------------


def test_generate_structured_returns_a_validated_model(fake_llm: FakeScript) -> None:
    fake_llm.queue(GOOD)
    result = generate_structured(Plan, PROMPT)
    assert isinstance(result, Plan)
    assert result.angle == "Consistency beats intensity"
    assert result.key_messages == ["Start small", "Track weekly"]
    assert len(fake_llm.calls) == 1


def test_generate_structured_parses_fenced_json(fake_llm: FakeScript) -> None:
    fake_llm.queue("```json\n" + Plan(**GOOD).model_dump_json() + "\n```")
    assert generate_structured(Plan, PROMPT).goal == "awareness"


def test_generate_structured_passes_kind_temperature_and_schema(
    fake_llm: FakeScript,
) -> None:
    fake_llm.queue(GOOD)
    generate_structured(Plan, PROMPT, kind="fast", temperature=0.5)
    (call,) = fake_llm.calls
    assert call.kind == "fast" and call.model == "fake-fast"
    assert call.temperature == 0.5
    assert call.json_schema == Plan.model_json_schema()


def test_generate_structured_uses_an_explicit_model(fake_llm: FakeScript) -> None:
    own = FakeScript().queue(GOOD)
    model = FakeChatModel(model="custom", kind="text", script=own)
    assert generate_structured(Plan, PROMPT, model=model).angle
    assert fake_llm.calls == [] and len(own.calls) == 1


def test_invalid_json_triggers_one_repair_retry(fake_llm: FakeScript) -> None:
    fake_llm.queue("this is not json at all", GOOD)
    result = generate_structured(Plan, PROMPT)
    assert result.angle == "Consistency beats intensity"
    first, second = fake_llm.calls
    assert first.messages == tuple(PROMPT)
    # repair prompt = original + the bad reply + a correction request
    assert second.messages[: len(PROMPT)] == tuple(PROMPT)
    assert second.messages[-2] == Message("assistant", "this is not json at all")
    assert second.messages[-1].role == "user"
    assert "not valid JSON" in second.messages[-1].content
    assert '"key_messages"' in second.messages[-1].content  # schema included


def test_schema_violation_triggers_repair_and_names_the_field(
    fake_llm: FakeScript,
) -> None:
    bad = {**GOOD, "key_messages": ["only one"]}
    fake_llm.queue(bad, GOOD)
    assert generate_structured(Plan, PROMPT).key_messages == GOOD["key_messages"]
    repair = fake_llm.calls[1].messages[-1].content
    assert "key_messages" in repair


def test_repair_prompt_does_not_echo_invalid_values_in_the_problem_text(
    fake_llm: FakeScript,
) -> None:
    fake_llm.queue({**GOOD, "goal": "TOPSECRETVALUE"}, GOOD)
    generate_structured(Plan, PROMPT)
    repair = fake_llm.calls[1].messages[-1].content
    assert "goal" in repair
    assert "TOPSECRETVALUE" not in repair


def test_only_one_repair_attempt_is_made(fake_llm: FakeScript) -> None:
    fake_llm.queue("bad one", "bad two", GOOD)
    with pytest.raises(LLMOutputError) as err:
        generate_structured(Plan, PROMPT)
    assert len(fake_llm.calls) == 2
    assert fake_llm.pending == 1  # the third (good) response was never requested
    assert err.value.code == "llm_invalid_output"
    assert err.value.details["attempts"] == 2  # type: ignore[index]
    assert err.value.details["schema"] == "Plan"  # type: ignore[index]


def test_output_error_never_contains_the_model_reply(fake_llm: FakeScript) -> None:
    fake_llm.queue("LEAKME-one", "LEAKME-two")
    with pytest.raises(LLMOutputError) as err:
        generate_structured(Plan, PROMPT)
    assert "LEAKME" not in repr(err.value.message)
    assert "LEAKME" not in repr(err.value.details)


def test_provider_failures_are_not_repaired_or_retried(fake_llm: FakeScript) -> None:
    fake_llm.fail_next(1).queue(GOOD)
    with pytest.raises(LLMProviderError):
        generate_structured(Plan, PROMPT)
    assert len(fake_llm.calls) == 1
    assert fake_llm.pending == 1


def test_provider_failure_during_repair_propagates(fake_llm: FakeScript) -> None:
    fake_llm.queue("bad", LLMProviderError("down"))
    with pytest.raises(LLMProviderError):
        generate_structured(Plan, PROMPT)
    assert len(fake_llm.calls) == 2


def test_generate_structured_rejects_empty_messages(fake_llm: FakeScript) -> None:
    with pytest.raises(ValueError):
        generate_structured(Plan, [])
    assert fake_llm.calls == []


def test_non_object_json_for_a_model_schema_is_repaired(fake_llm: FakeScript) -> None:
    fake_llm.queue("[1, 2, 3]", GOOD)
    assert generate_structured(Plan, PROMPT).angle


# --------------------------------------------------------------------------
# Usage capture
# --------------------------------------------------------------------------


def test_capture_usage_records_tokens_model_latency_and_cost(
    fake_llm: FakeScript,
) -> None:
    fake_llm.queue("one two three")
    with capture_usage() as usage:
        get_chat_model().invoke([user_message("a b c d")])
    assert usage.calls == 1
    record = usage.records[0]
    assert record.provider == "fake" and record.model == "fake-text"
    assert record.kind == "text"
    assert record.tokens_in == 4 and record.tokens_out == 3
    assert record.cost_usd == 0.0
    assert record.latency_ms >= 0
    assert usage.tokens_in == 4 and usage.tokens_out == 3
    assert usage.latency_ms == record.latency_ms
    assert usage.cost_usd == 0.0
    assert usage.models == ("fake-text",)


def test_capture_usage_counts_the_repair_call(fake_llm: FakeScript) -> None:
    fake_llm.queue("bad", GOOD)
    with capture_usage() as usage:
        generate_structured(Plan, PROMPT)
    assert usage.calls == 2


def test_capture_usage_ignores_failed_calls(fake_llm: FakeScript) -> None:
    fake_llm.fail_next(1).queue("fine")
    model = get_chat_model()
    with capture_usage() as usage:
        with pytest.raises(LLMProviderError):
            model.invoke(PROMPT)
        model.invoke(PROMPT)
    assert usage.calls == 1


def test_capture_usage_is_scoped_and_nestable(fake_llm: FakeScript) -> None:
    fake_llm.set_default("x")
    model = get_chat_model()
    model.invoke(PROMPT)  # before: nobody listening
    with capture_usage() as outer:
        model.invoke(PROMPT)
        with capture_usage() as inner:
            model.invoke(PROMPT)
        model.invoke(PROMPT)  # after inner closed
    model.invoke(PROMPT)  # after outer closed
    assert outer.calls == 3
    assert inner.calls == 1


def test_capture_usage_resets_after_an_exception(fake_llm: FakeScript) -> None:
    fake_llm.set_default("x")
    with pytest.raises(RuntimeError):
        with capture_usage():
            raise RuntimeError
    with capture_usage() as usage:
        get_chat_model().invoke(PROMPT)
    assert usage.calls == 1


def test_cost_is_none_when_no_call_reports_one() -> None:
    class Free(ChatModel):
        provider = "free"

        def _generate(self, messages, *, temperature, json_schema):  # type: ignore[no-untyped-def]
            return LLMCompletion(text="x")

    with capture_usage() as usage:
        Free(model="m", kind="text").invoke(PROMPT)
    assert usage.cost_usd is None


def test_capture_usage_does_not_leak_into_other_threads(fake_llm: FakeScript) -> None:
    fake_llm.set_default("x")
    other_calls: list[int] = []

    def work() -> None:
        with capture_usage() as mine:
            get_chat_model().invoke(PROMPT)
        other_calls.append(mine.calls)

    with capture_usage() as usage:
        t = threading.Thread(target=work)
        t.start()
        t.join()
    assert other_calls == [1]
    assert usage.calls == 0


# --------------------------------------------------------------------------
# Privacy: prompts and replies are never logged
# --------------------------------------------------------------------------


def test_prompts_and_replies_are_not_logged(
    fake_llm: FakeScript, caplog: pytest.LogCaptureFixture
) -> None:
    fake_llm.queue("REPLY-SECRET-1", "REPLY-SECRET-2")
    with caplog.at_level(logging.DEBUG, logger="app.agents.llm"):
        with pytest.raises(LLMOutputError):
            generate_structured(Plan, [user_message("PROMPT-SECRET")])
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "LLM call ok" in text
    assert "SECRET" not in text
