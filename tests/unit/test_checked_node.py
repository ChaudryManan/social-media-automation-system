"""Tests for ``app.agents.checked`` (B2-04): agent -> check -> retry/escalate."""

import io
import json
import logging
import uuid
from collections.abc import Callable
from typing import Any

import pytest
import sqlalchemy as sa
from app.agents import checked
from app.agents.checked import (
    AgentOutput,
    CheckResult,
    Issue,
    StepRecord,
    add_checked_node,
    build_feedback,
    make_checked_node,
    node_name,
)
from app.agents.llm import (
    FakeScript,
    LLMConfigError,
    LLMOutputError,
    LLMProviderError,
    generate_structured,
    user_message,
)
from app.agents.prompts import load_prompt
from app.agents.state import GraphState
from app.core.errors import ConflictError
from app.core.logging import JSONFormatter, attempt_ctx, node_ctx, run_id_ctx
from app.models import Base
from app.services import runs
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.orm import Session, sessionmaker

KEY = "writer"


# --------------------------------------------------------------------------
# Test harness
# --------------------------------------------------------------------------


class Harness:
    """A one-step graph: ``writer`` (agent, check, escalate) then ``done``."""

    def __init__(
        self,
        agent_fn: Callable[[GraphState], Any],
        check_fn: Callable[[GraphState], CheckResult],
        *,
        key: str = KEY,
        max_retries: int = 3,
        judge_fn: Callable[[GraphState], CheckResult] | None = None,
        judge_threshold: float = 0.7,
    ) -> None:
        self.steps: list[StepRecord] = []
        self.done_runs = 0
        self.checkpointer = InMemorySaver()
        self.checked = make_checked_node(
            key,
            agent_fn,
            check_fn,
            max_retries,
            judge_fn=judge_fn,
            judge_threshold=judge_threshold,
        )
        self.config = {
            "configurable": {
                "thread_id": str(uuid.uuid4()),
                "step_recorder": self.steps.append,
            }
        }
        self.graph = self.compile()

    def _done(self, state: GraphState) -> dict[str, Any]:
        self.done_runs += 1
        return {"status": "done"}

    def compile(self) -> Any:
        builder = StateGraph(GraphState)
        builder.add_node("done", self._done)
        entry = add_checked_node(builder, self.checked, on_pass="done")
        builder.add_edge(START, entry)
        builder.add_edge("done", END)
        return builder.compile(checkpointer=self.checkpointer)

    def run(self, state: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.graph.invoke(state or {"topic": "t"}, self.config)

    def resume(self, value: Any) -> dict[str, Any]:
        return self.graph.invoke(Command(resume=value), self.config)

    @property
    def verdicts(self) -> list[str]:
        return [s.verdict for s in self.steps]

    @property
    def attempts(self) -> list[int]:
        return [s.attempt for s in self.steps]


def ok() -> CheckResult:
    return CheckResult(ok=True)


def bad(message: str = "Caption is too long. Shorten it.") -> CheckResult:
    return CheckResult.from_issues(
        [Issue(code="too_long", message=message, severity="error")]
    )


def scripted_check(*results: CheckResult) -> tuple[Callable, list[GraphState]]:
    """A check_fn that returns ``results`` in order and remembers the states."""
    queue = list(results)
    seen: list[GraphState] = []

    def check(state: GraphState) -> CheckResult:
        seen.append(state)
        return queue.pop(0)

    return check, seen


class Agent:
    """Agent that records the feedback it was given on each call."""

    def __init__(self, fn: Callable[[int], Any] | None = None) -> None:
        self.feedback_seen: list[str | None] = []
        self.fn = fn or (lambda n: {"drafts": {"facebook": {"caption": f"v{n}"}}})

    def __call__(self, state: GraphState) -> Any:
        self.feedback_seen.append((state.get("feedback") or {}).get(KEY))
        return self.fn(len(self.feedback_seen))


# --------------------------------------------------------------------------
# The four scenarios from TASKS.md
# --------------------------------------------------------------------------


def test_passes_first_time() -> None:
    agent = Agent()
    check, _ = scripted_check(ok())
    h = Harness(agent, check)

    result = h.run()

    assert result["status"] == "done"
    assert "__interrupt__" not in result
    assert len(agent.feedback_seen) == 1
    assert h.verdicts == ["passed"]
    assert h.attempts == [1]
    assert (result.get("retries") or {}).get(KEY, 0) == 0
    assert result["feedback"][KEY] is None
    assert result["drafts"]["facebook"]["caption"] == "v1"
    assert h.done_runs == 1


def test_fails_twice_then_passes() -> None:
    agent = Agent()
    check, _ = scripted_check(bad("Fix A."), bad("Fix B."), ok())
    h = Harness(agent, check)

    result = h.run()

    assert result["status"] == "done"
    assert h.verdicts == ["retried", "retried", "passed"]
    assert h.attempts == [1, 2, 3]
    assert agent.feedback_seen == [None, "Fix A.", "Fix B."]
    assert result["retries"][KEY] == 2
    assert result["feedback"][KEY] is None
    assert result["drafts"]["facebook"]["caption"] == "v3"
    assert h.steps[0].feedback == "Fix A."
    assert h.steps[2].feedback is None
    assert [s.input_summary["retries_before"] for s in h.steps] == [0, 1, 2]


def test_exhausting_retries_escalates_with_an_interrupt() -> None:
    agent = Agent()
    check, _ = scripted_check(bad("A."), bad("B."), bad("C."))
    h = Harness(agent, check, max_retries=3)

    result = h.run()

    assert len(agent.feedback_seen) == 3
    assert h.verdicts == ["retried", "retried", "escalated"]
    assert h.done_runs == 0
    (interrupt,) = result["__interrupt__"]
    payload = interrupt.value
    assert payload["type"] == "needs_attention"
    assert payload["step"] == KEY
    assert payload["reason"] == "retries_exhausted"
    assert payload["attempts"] == 3
    assert payload["max_retries"] == 3
    assert payload["feedback"] == "C."
    assert payload["issues"][0]["code"] == "too_long"
    assert result["retries"][KEY] == 3


def test_resume_with_retry_resets_the_counter() -> None:
    agent = Agent()
    check, _ = scripted_check(bad("A."), bad("B."), bad("C."), bad("D."), ok())
    h = Harness(agent, check, max_retries=3)
    h.run()

    result = h.resume({"action": "retry"})

    # counter was reset to 0, so one failed attempt after resume is a normal
    # retry again (it would have escalated at once without the reset)
    assert h.verdicts == ["retried", "retried", "escalated", "retried", "passed"]
    assert h.attempts == [1, 2, 3, 4, 5]  # attempt numbers keep counting up
    assert result["status"] == "done"
    assert result["retries"][KEY] == 1
    assert agent.feedback_seen[3] == "C."  # feedback survived the escalation


def test_resume_with_retry_allows_a_full_new_round_before_escalating_again() -> None:
    agent = Agent()
    check, _ = scripted_check(*[bad(f"F{i}.") for i in range(6)])
    h = Harness(agent, check, max_retries=3)
    h.run()

    result = h.resume({"action": "retry"})

    assert len(agent.feedback_seen) == 6
    assert h.verdicts[-1] == "escalated"
    assert result["__interrupt__"][0].value["attempts"] == 6
    # the retry resolution put the workflow back to "generating"
    assert h.graph.get_state(h.config).values["status"] == "generating"


def test_resume_with_abort_ends_the_graph_as_failed() -> None:
    agent = Agent()
    check, _ = scripted_check(bad("A."), bad("B."), bad("C."))
    h = Harness(agent, check)
    h.run()

    result = h.resume({"action": "abort"})

    assert result["status"] == "failed"
    assert result["errors"] == [f"Step '{KEY}' was aborted by a human after attempt 3."]
    assert h.done_runs == 0
    assert h.verdicts == ["retried", "retried", "escalated", "failed"]
    assert h.steps[-1].attempt == 3
    assert "__interrupt__" not in result


@pytest.mark.parametrize(
    "value", ["retry", {"action": "skip"}, {"action": None}, {"x": 1}, ["retry"]]
)
def test_an_invalid_resume_value_asks_again_instead_of_failing(value: Any) -> None:
    check, _ = scripted_check(bad("A."), ok())
    h = Harness(Agent(), check, max_retries=1)
    h.run()

    result = h.resume(value)

    (interrupt,) = result["__interrupt__"]
    assert interrupt.value["type"] == "needs_attention"
    assert interrupt.value["error"] == "invalid_resolution"
    assert interrupt.value["allowed"] == ["retry", "abort"]
    assert h.done_runs == 0
    assert h.verdicts == ["escalated"]  # nothing was resolved or recorded


def test_a_valid_resume_after_invalid_ones_still_works() -> None:
    check, _ = scripted_check(bad("A."), ok())
    h = Harness(Agent(), check, max_retries=1)
    h.run()
    h.resume({"action": "nope"})
    h.resume("retry")

    result = h.resume({"action": "retry"})

    assert result["status"] == "done"
    assert h.verdicts == ["escalated", "passed"]


def test_an_invalid_resume_followed_by_abort_aborts() -> None:
    check, _ = scripted_check(bad("A."))
    h = Harness(Agent(), check, max_retries=1)
    h.run()
    h.resume({"action": "nope"})

    result = h.resume({"action": "abort"})

    assert result["status"] == "failed"
    assert h.verdicts == ["escalated", "failed"]


def test_escalation_survives_a_new_graph_object_with_the_same_checkpointer() -> None:
    """A worker restart builds the graph again; the checkpoint is what counts."""
    check, _ = scripted_check(bad("A."), ok())
    h = Harness(Agent(), check, max_retries=1)
    h.run()
    h.graph = h.compile()  # fresh object, same checkpointer and thread

    result = h.resume({"action": "retry"})

    assert result["status"] == "done"
    assert h.verdicts == ["escalated", "passed"]


# --------------------------------------------------------------------------
# Feedback reaches the next attempt's prompt (fake LLM + prompt loader)
# --------------------------------------------------------------------------


class Hashtags(BaseModel):
    hashtags: list[str]


def test_retry_feedback_changes_the_second_prompt(fake_llm: FakeScript) -> None:
    template = load_prompt("hashtag")
    fake_llm.queue({"hashtags": ["#one"]}, {"hashtags": ["#one", "#two"]})

    def agent(state: GraphState) -> AgentOutput:
        messages = template.messages(
            feedback=(state.get("feedback") or {}).get(KEY),
            platform="instagram",
            hashtag_count=2,
            hashtag_min=2,
            hashtag_max=8,
            banned_words=[],
            language="en",
            caption="Hello",
            plan={},
        )
        out = generate_structured(Hashtags, messages)
        return AgentOutput(
            update={"drafts": {"instagram": {"hashtags": out.hashtags}}},
            prompt_version=template.version,
        )

    def check(state: GraphState) -> CheckResult:
        tags = state["drafts"]["instagram"]["hashtags"]
        if len(tags) < 2:
            return bad("Use at least 2 hashtags.")
        return ok()

    h = Harness(agent, check)

    result = h.run()

    first, second = fake_llm.calls
    assert "Previous attempt feedback" not in first.prompt_text
    assert "Previous attempt feedback" in second.prompt_text
    assert "Use at least 2 hashtags." in second.prompt_text
    assert result["drafts"]["instagram"]["hashtags"] == ["#one", "#two"]
    assert h.verdicts == ["retried", "passed"]
    assert h.steps[0].output_summary["prompt_version"] == template.version
    assert h.steps[1].input_summary["prior_feedback"] == "Use at least 2 hashtags."


def test_usage_of_agent_and_judge_is_summed_into_the_step(fake_llm: FakeScript) -> None:
    fake_llm.queue({"hashtags": ["#a b c"]}, {"hashtags": ["#a"]})

    def agent(state: GraphState) -> dict[str, Any]:
        out = generate_structured(Hashtags, [user_message("write")])
        return {"drafts": {"x": {"hashtags": out.hashtags}}}

    def judge(state: GraphState) -> CheckResult:
        generate_structured(Hashtags, [user_message("judge")])
        return CheckResult(ok=True, score=0.9)

    h = Harness(agent, lambda s: ok(), judge_fn=judge)

    h.run()

    (step,) = h.steps
    assert step.tokens_in == 2  # one word in each of the two prompts
    expected_out = sum(
        len(json.dumps(reply).split())
        for reply in ({"hashtags": ["#a b c"]}, {"hashtags": ["#a"]})
    )
    assert step.tokens_out == expected_out
    assert step.model == "fake-text"
    assert step.cost_usd == 0.0
    assert len(fake_llm.calls) == 2


# --------------------------------------------------------------------------
# Rule checks first, then the judge
# --------------------------------------------------------------------------


def test_judge_is_not_called_when_the_rules_fail() -> None:
    judge_calls: list[int] = []

    def judge(state: GraphState) -> CheckResult:
        judge_calls.append(1)
        return CheckResult(ok=True, score=1.0)

    check, _ = scripted_check(bad("Rule."), ok())
    h = Harness(Agent(), check, judge_fn=judge)

    h.run()

    assert judge_calls == [1]  # only the second attempt got past the rules
    assert h.verdicts == ["retried", "passed"]


def test_judge_failure_feedback_goes_to_the_agent() -> None:
    agent = Agent()
    judge_results = [
        CheckResult.from_issues(
            [
                Issue(
                    code="weak_hook",
                    message="Make the hook stronger.",
                    severity="error",
                )
            ],
            score=0.4,
            keep="the tone",
        ),
        CheckResult(ok=True, score=0.9),
    ]
    h = Harness(agent, lambda s: ok(), judge_fn=lambda s: judge_results.pop(0))

    h.run()

    assert agent.feedback_seen == [None, "Make the hook stronger. Keep: the tone"]
    assert h.verdicts == ["retried", "passed"]
    assert h.steps[0].output_summary["score"] == 0.4


@pytest.mark.parametrize(
    ("score", "passes"),
    [(0.69, False), (0.7, True), (0.71, True), (0.0, False), (1.0, True)],
)
def test_judge_threshold(score: float, passes: bool) -> None:
    h = Harness(
        Agent(),
        lambda s: ok(),
        judge_fn=lambda s: CheckResult(ok=True, score=score),
        max_retries=1,
    )

    result = h.run()

    assert (h.verdicts == ["passed"]) is passes
    assert ("__interrupt__" in result) is not passes


def test_judge_threshold_is_configurable() -> None:
    h = Harness(
        Agent(),
        lambda s: ok(),
        judge_fn=lambda s: CheckResult(ok=True, score=0.5),
        judge_threshold=0.5,
    )

    h.run()

    assert h.verdicts == ["passed"]


def test_low_judge_score_adds_a_feedback_the_agent_can_act_on() -> None:
    agent = Agent()
    scores = [0.3, 0.95]
    h = Harness(
        agent,
        lambda s: ok(),
        judge_fn=lambda s: CheckResult(ok=True, score=scores.pop(0)),
    )

    h.run()

    assert "0.30" in agent.feedback_seen[1]
    assert "0.70" in agent.feedback_seen[1]
    codes = [i["code"] for i in h.steps[0].output_summary["issues"]]
    assert codes == ["judge_score_low"]


def test_judge_warnings_do_not_block_and_are_recorded() -> None:
    warning = Issue(code="meh", message="Slightly generic.", severity="warning")
    h = Harness(
        Agent(),
        lambda s: CheckResult(ok=True, issues=[warning]),
        judge_fn=lambda s: CheckResult(ok=True, score=0.9, issues=[warning]),
    )

    h.run()

    assert h.verdicts == ["passed"]
    assert len(h.steps[0].output_summary["issues"]) == 2


def test_a_failing_check_without_any_feedback_gets_a_generic_one() -> None:
    agent = Agent()
    check, _ = scripted_check(CheckResult(ok=False), ok())
    h = Harness(agent, check)

    h.run()

    assert agent.feedback_seen[1]
    assert "did not pass" in agent.feedback_seen[1]


def test_check_functions_must_return_a_check_result() -> None:
    h = Harness(Agent(), lambda s: {"ok": True})  # type: ignore[arg-type,return-value]

    with pytest.raises(TypeError, match="CheckResult"):
        h.run()

    assert h.verdicts == ["error"]


def test_judge_functions_must_return_a_check_result() -> None:
    h = Harness(Agent(), lambda s: ok(), judge_fn=lambda s: None)  # type: ignore[arg-type,return-value]

    with pytest.raises(TypeError, match="CheckResult"):
        h.run()


def test_check_receives_the_state_the_agent_produced() -> None:
    check, seen = scripted_check(ok())
    h = Harness(Agent(), check)

    h.run({"topic": "tea", "language": "en"})

    assert seen[0]["drafts"]["facebook"]["caption"] == "v1"
    assert seen[0]["topic"] == "tea"


# --------------------------------------------------------------------------
# Errors, refusals and escalation without retry
# --------------------------------------------------------------------------


def test_a_provider_error_counts_as_a_failed_attempt_and_is_retried() -> None:
    def flaky(n: int) -> Any:
        if n == 1:
            raise LLMProviderError("down", retriable=True)
        return {"drafts": {"facebook": {"caption": "ok"}}}

    agent = Agent(flaky)
    check, _ = scripted_check(ok())
    h = Harness(agent, check)

    result = h.run()

    assert result["status"] == "done"
    assert h.verdicts == ["error", "passed"]
    assert h.steps[0].feedback == "Error: llm_provider_error"
    assert h.steps[0].output_summary["error_code"] == "llm_provider_error"
    assert result["retries"][KEY] == 1
    assert agent.feedback_seen == [None, None]  # an outage gives no advice


def test_an_error_keeps_the_previous_checker_feedback() -> None:
    def fn(n: int) -> Any:
        if n == 2:
            raise LLMOutputError()
        return {"drafts": {"facebook": {"caption": f"v{n}"}}}

    agent = Agent(fn)
    check, _ = scripted_check(bad("Shorten it."), ok())
    h = Harness(agent, check)

    h.run()

    assert h.verdicts == ["retried", "error", "passed"]
    assert agent.feedback_seen == [None, "Shorten it.", "Shorten it."]


def test_repeated_errors_exhaust_the_retries_and_escalate() -> None:
    def always(n: int) -> Any:
        raise LLMProviderError("down", retriable=True)

    h = Harness(Agent(always), lambda s: ok(), max_retries=2)

    result = h.run()

    assert h.verdicts == ["error", "escalated"]
    assert result["__interrupt__"][0].value["reason"] == "retries_exhausted"
    assert result["__interrupt__"][0].value["error_code"] == "llm_provider_error"


@pytest.mark.parametrize(
    "error",
    [
        LLMProviderError("quota", retriable=False),
        LLMConfigError(),
    ],
)
def test_a_non_retriable_error_escalates_immediately(error: Exception) -> None:
    def fail(n: int) -> Any:
        raise error

    agent = Agent(fail)
    h = Harness(agent, lambda s: ok(), max_retries=3)

    result = h.run()

    assert len(agent.feedback_seen) == 1
    assert h.verdicts == ["escalated"]
    assert result["__interrupt__"][0].value["reason"] == "non_retriable_error"


def test_a_refusal_escalates_immediately_without_running_the_check() -> None:
    check_calls: list[int] = []

    def check(state: GraphState) -> CheckResult:
        check_calls.append(1)
        return ok()

    agent = Agent(lambda n: AgentOutput(update={}, refused_reason="unsafe topic"))
    h = Harness(agent, check, max_retries=3)

    result = h.run()

    assert check_calls == []
    assert h.verdicts == ["escalated"]
    assert h.steps[0].feedback == "Refused: unsafe topic"
    payload = result["__interrupt__"][0].value
    assert payload["reason"] == "refused"
    assert payload["refused"] == "unsafe topic"


def test_a_failing_judge_call_is_an_error_attempt() -> None:
    calls = {"n": 0}

    def judge(state: GraphState) -> CheckResult:
        calls["n"] += 1
        if calls["n"] == 1:
            raise LLMProviderError("judge down", retriable=True)
        return CheckResult(ok=True, score=0.9)

    h = Harness(Agent(), lambda s: ok(), judge_fn=judge)

    h.run()

    assert h.verdicts == ["error", "passed"]


def test_unexpected_exceptions_are_recorded_and_re_raised() -> None:
    def broken(n: int) -> Any:
        raise KeyError("boom")

    h = Harness(Agent(broken), lambda s: ok())

    with pytest.raises(KeyError):
        h.run()

    assert h.verdicts == ["error"]
    assert h.steps[0].feedback == "Unexpected KeyError"
    assert h.steps[0].output_summary == {"error_type": "KeyError"}


def test_unexpected_exceptions_in_the_check_are_recorded_and_re_raised() -> None:
    def check(state: GraphState) -> CheckResult:
        raise ZeroDivisionError

    h = Harness(Agent(), check)

    with pytest.raises(ZeroDivisionError):
        h.run()

    assert h.verdicts == ["error"]
    assert h.steps[0].feedback == "Unexpected ZeroDivisionError in check"


@pytest.mark.parametrize("field", ["retries", "feedback", "attempt_info"])
def test_agents_cannot_write_the_retry_bookkeeping(field: str) -> None:
    h = Harness(Agent(lambda n: {field: {}}), lambda s: ok())

    with pytest.raises(ValueError, match="bookkeeping"):
        h.run()


def test_the_check_node_cannot_run_before_the_agent() -> None:
    node = make_checked_node(KEY, lambda s: {}, lambda s: ok())

    with pytest.raises(RuntimeError, match="before its agent"):
        node.check({}, {})


def test_route_functions_refuse_to_guess() -> None:
    node = make_checked_node(KEY, lambda s: {}, lambda s: ok())

    with pytest.raises(RuntimeError, match="before its check"):
        node.route({})
    with pytest.raises(RuntimeError, match="no resolution"):
        node.route_resolution({})


@pytest.mark.parametrize("outcome", ["pass", "retry", "escalate"])
def test_route_returns_the_outcome_set_by_the_check(outcome: str) -> None:
    node = make_checked_node(KEY, lambda s: {}, lambda s: ok())

    assert node.route({"attempt_info": {KEY: {"outcome": outcome}}}) == outcome


# --------------------------------------------------------------------------
# decide()
# --------------------------------------------------------------------------


def _case(passed: bool, retries_after: int, max_retries: int = 3, **kw: bool) -> dict:
    return {
        "passed": passed,
        "retriable": kw.get("retriable", True),
        "refused": kw.get("refused", False),
        "retries_after": retries_after,
        "max_retries": max_retries,
    }


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (_case(True, 0), "pass"),
        (_case(False, 1), "retry"),
        (_case(False, 2), "retry"),
        (_case(False, 3), "escalate"),
        (_case(False, 1, max_retries=1), "escalate"),
        (_case(False, 1, retriable=False), "escalate"),
        (_case(False, 1, refused=True), "escalate"),
    ],
)
def test_decide(kwargs: dict[str, Any], expected: str) -> None:
    assert checked.decide(**kwargs) == expected


# --------------------------------------------------------------------------
# Limits, keys and wiring
# --------------------------------------------------------------------------


def test_max_retries_of_one_escalates_on_the_first_failure() -> None:
    agent = Agent()
    check, _ = scripted_check(bad("A."))
    h = Harness(agent, check, max_retries=1)

    result = h.run()

    assert len(agent.feedback_seen) == 1
    assert h.verdicts == ["escalated"]
    assert "__interrupt__" in result


def test_scene_keys_work_as_step_names() -> None:
    results = [bad("A."), ok()]
    node = make_checked_node(
        "scene:2", lambda s: {"drafts": {}}, lambda s: results.pop(0), 2
    )
    steps: list[StepRecord] = []
    builder = StateGraph(GraphState)
    entry = add_checked_node(builder, node, on_pass=END)
    builder.add_edge(START, entry)
    graph = builder.compile(checkpointer=InMemorySaver())

    result = graph.invoke(
        {}, {"configurable": {"thread_id": "t", "step_recorder": steps.append}}
    )

    assert node.agent_name == "scene_2"
    assert node.check_name == "check_scene_2"
    assert node.escalate_name == "escalate_scene_2"
    assert {s.node_name for s in steps} == {"scene:2"}
    assert result["retries"]["scene:2"] == 1


def test_node_name_replaces_reserved_characters() -> None:
    assert node_name("writer") == "writer"
    assert node_name("scene:2") == "scene_2"
    assert node_name("a|b c") == "a_b_c"


def test_two_checked_nodes_keep_separate_counters_and_feedback() -> None:
    log: list[str] = []

    def agent_a(state: GraphState) -> dict[str, Any]:
        log.append(f"a:{(state.get('feedback') or {}).get('a')}")
        return {"plan": {"x": 1}}

    def agent_b(state: GraphState) -> dict[str, Any]:
        log.append(f"b:{(state.get('feedback') or {}).get('b')}")
        return {"research": {"y": 2}}

    checks_a = [ok()]
    checks_b = [bad("B-fix."), ok()]
    node_a = make_checked_node("a", agent_a, lambda s: checks_a.pop(0))
    node_b = make_checked_node("b", agent_b, lambda s: checks_b.pop(0))
    steps: list[StepRecord] = []

    builder = StateGraph(GraphState)
    entry_a = add_checked_node(builder, node_a, on_pass=node_b.agent_name)
    add_checked_node(builder, node_b, on_pass=END)
    builder.add_edge(START, entry_a)
    graph = builder.compile(checkpointer=InMemorySaver())

    result = graph.invoke(
        {"topic": "t"},
        {"configurable": {"thread_id": "x", "step_recorder": steps.append}},
    )

    assert log == ["a:None", "b:None", "b:B-fix."]
    assert result["retries"] == {"b": 1}
    assert result["feedback"] == {"a": None, "b": None}
    assert [(s.node_name, s.verdict) for s in steps] == [
        ("a", "passed"),
        ("b", "retried"),
        ("b", "passed"),
    ]


def test_on_abort_can_point_at_another_node() -> None:
    check, _ = scripted_check(bad("A."))
    checked_node = make_checked_node(KEY, Agent(), check, 1)
    visited: list[str] = []
    builder = StateGraph(GraphState)
    builder.add_node("cleanup", lambda s: visited.append("cleanup") or {})
    entry = add_checked_node(builder, checked_node, on_pass=END, on_abort="cleanup")
    builder.add_edge(START, entry)
    builder.add_edge("cleanup", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "t"}}
    graph.invoke({}, config)

    result = graph.invoke(Command(resume={"action": "abort"}), config)

    assert visited == ["cleanup"]
    assert result["status"] == "failed"


@pytest.mark.parametrize("key", ["", "   "])
def test_make_checked_node_rejects_an_empty_key(key: str) -> None:
    with pytest.raises(ValueError, match="key"):
        make_checked_node(key, lambda s: {}, lambda s: ok())


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "3"])
def test_make_checked_node_rejects_bad_retry_limits(value: Any) -> None:
    with pytest.raises(ValueError, match="max_retries"):
        make_checked_node(KEY, lambda s: {}, lambda s: ok(), value)


@pytest.mark.parametrize("value", [-0.1, 1.1])
def test_make_checked_node_rejects_bad_thresholds(value: float) -> None:
    with pytest.raises(ValueError, match="judge_threshold"):
        make_checked_node(KEY, lambda s: {}, lambda s: ok(), judge_threshold=value)


def test_default_limits_match_the_documented_settings() -> None:
    assert checked.DEFAULT_MAX_RETRIES == 3
    assert checked.DEFAULT_SCENE_MAX_RETRIES == 2
    assert checked.DEFAULT_JUDGE_PASS_THRESHOLD == 0.7
    node = make_checked_node(KEY, lambda s: {}, lambda s: ok())
    assert node.max_retries == 3


# --------------------------------------------------------------------------
# Step content
# --------------------------------------------------------------------------


def test_step_rows_carry_the_documented_fields() -> None:
    def agent(state: GraphState) -> AgentOutput:
        return AgentOutput(
            update={"drafts": {}},
            prompt_version="writer.v1",
            summary={"chars": 120, "api_key": "sekret"},
        )

    h = Harness(agent, lambda s: CheckResult(ok=True))

    h.run()

    (step,) = h.steps
    assert step.node_name == KEY
    assert step.attempt == 1
    assert step.verdict == "passed"
    assert step.feedback is None
    assert step.output_summary["prompt_version"] == "writer.v1"
    assert step.output_summary["summary"]["chars"] == 120
    assert "sekret" not in json.dumps(step.output_summary)  # redacted
    assert step.output_summary["outcome"] == "pass"
    assert step.output_summary["duration_ms"] >= 0
    assert step.tokens_in == step.tokens_out == step.latency_ms == 0
    assert step.cost_usd is None
    assert step.model is None


def test_attempt_info_is_json_serialisable() -> None:
    h = Harness(Agent(), lambda s: ok())

    result = h.run()

    info = result["attempt_info"][KEY]
    json.dumps(info)
    assert info["attempt"] == 1
    assert info["outcome"] == "pass"
    assert info["error"] is None


def test_a_second_pass_through_the_same_step_keeps_counting_attempts() -> None:
    """Reviewer re-entry runs the step again in the same run (AGENT_SPECS 5.2)."""
    agent = Agent()
    check, _ = scripted_check(ok(), ok())
    h = Harness(agent, check)
    h.run()

    first = h.checked.agent({"attempt_info": {KEY: {"attempt": 1}}}, h.config)

    assert first["attempt_info"][KEY]["attempt"] == 2


# --------------------------------------------------------------------------
# Recorder resolution and the database recorder
# --------------------------------------------------------------------------


def test_without_a_recorder_the_graph_still_runs() -> None:
    check, _ = scripted_check(ok())
    node = make_checked_node(KEY, Agent(), check)
    builder = StateGraph(GraphState)
    entry = add_checked_node(builder, node, on_pass=END)
    builder.add_edge(START, entry)
    graph = builder.compile(checkpointer=InMemorySaver())

    result = graph.invoke({}, {"configurable": {"thread_id": "t"}})

    assert result["attempt_info"][KEY]["outcome"] == "pass"


def test_recorder_resolution_order(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = uuid.uuid4()
    sentinel = object()
    monkeypatch.setattr(checked, "db_step_recorder", lambda rid: ("db", rid))

    assert checked.resolve_step_recorder(None) is None
    assert checked.resolve_step_recorder({}) is None
    assert checked.resolve_step_recorder({"configurable": {}}) is None
    assert checked.resolve_step_recorder({"configurable": {"run_id": str(run_id)}}) == (
        "db",
        run_id,
    )
    assert (
        checked.resolve_step_recorder(
            {"configurable": {"run_id": str(run_id), "step_recorder": sentinel}}
        )
        is sentinel
    )


def test_a_recorder_that_fails_does_not_break_the_workflow(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken(step: StepRecord) -> None:
        raise RuntimeError("db is down")

    check, _ = scripted_check(ok())
    node = make_checked_node(KEY, Agent(), check)
    builder = StateGraph(GraphState)
    entry = add_checked_node(builder, node, on_pass=END)
    builder.add_edge(START, entry)
    graph = builder.compile(checkpointer=InMemorySaver())

    with caplog.at_level(logging.ERROR, logger="app.agents.checked"):
        result = graph.invoke(
            {}, {"configurable": {"thread_id": "t", "step_recorder": broken}}
        )

    assert result["attempt_info"][KEY]["outcome"] == "pass"
    assert "Could not record agent step" in caplog.text


@pytest.fixture
def sqlite_factory() -> Any:
    engine = sa.create_engine("sqlite:///:memory:", poolclass=sa.pool.StaticPool)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    yield factory
    engine.dispose()


def test_db_recorder_writes_one_agent_steps_row_per_attempt(
    sqlite_factory: Callable[[], Session],
) -> None:
    with sqlite_factory() as db:
        run = runs.start_run(db, uuid.uuid4())
        db.commit()
        run_id, request_id = run.id, run.request_id

    recorder = checked.db_step_recorder(run_id, sqlite_factory)
    agent = Agent()
    check, _ = scripted_check(bad("Shorten."), ok())
    node = make_checked_node(KEY, agent, check)
    builder = StateGraph(GraphState)
    entry = add_checked_node(builder, node, on_pass=END)
    builder.add_edge(START, entry)
    graph = builder.compile(checkpointer=InMemorySaver())

    graph.invoke({}, {"configurable": {"thread_id": "t", "step_recorder": recorder}})

    with sqlite_factory() as db:
        rows = runs.list_steps(db, run_id)
        assert [(r.node_name, r.attempt, r.verdict) for r in rows] == [
            (KEY, 1, "retried"),
            (KEY, 2, "passed"),
        ]
        assert rows[0].feedback == "Shorten."
        assert rows[0].request_id == request_id
        assert rows[0].input_summary["prior_feedback"] is None
        assert rows[1].input_summary["prior_feedback"] == "Shorten."
        assert rows[0].output_summary["outcome"] == "retry"
        assert rows[0].output_summary["issues"][0]["code"] == "too_long"


def test_db_recorder_refuses_a_finished_run(
    sqlite_factory: Callable[[], Session],
) -> None:
    with sqlite_factory() as db:
        run = runs.start_run(db, uuid.uuid4())
        runs.set_run_status(db, run, "completed")
        db.commit()
        run_id = run.id
    recorder = checked.db_step_recorder(run_id, sqlite_factory)
    step = StepRecord(KEY, 1, "passed", None, None, None, 0, 0, None, 0, None)

    with pytest.raises(ConflictError):
        recorder(step)


def test_run_summary_counts_the_retry_loop(
    sqlite_factory: Callable[[], Session],
) -> None:
    with sqlite_factory() as db:
        run = runs.start_run(db, uuid.uuid4())
        db.commit()
        run_id = run.id
    recorder = checked.db_step_recorder(run_id, sqlite_factory)
    check, _ = scripted_check(bad("A."), bad("B."), bad("C."))
    node = make_checked_node(KEY, Agent(), check, 3)
    builder = StateGraph(GraphState)
    entry = add_checked_node(builder, node, on_pass=END)
    builder.add_edge(START, entry)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "t", "step_recorder": recorder}}
    graph.invoke({}, config)
    graph.invoke(Command(resume={"action": "abort"}), config)

    with sqlite_factory() as db:
        run = runs.get_run(db, run_id)
        runs.set_run_status(db, run, "failed")
        summary = run.summary

    assert summary["verdicts"] == {"retried": 2, "escalated": 1, "failed": 1}
    assert summary["retries"] == 2
    assert summary["escalations"] == 1


# --------------------------------------------------------------------------
# Logging context
# --------------------------------------------------------------------------


def test_log_lines_inside_an_attempt_carry_node_attempt_and_run_id() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JSONFormatter())
    probe = logging.getLogger("probe.inside.agent")
    probe.addHandler(handler)
    probe.setLevel(logging.INFO)
    probe.propagate = False
    run_id = str(uuid.uuid4())

    def agent(state: GraphState) -> dict[str, Any]:
        probe.info("working")
        return {}

    check, _ = scripted_check(bad("A."), ok())
    node = make_checked_node(KEY, agent, check)
    builder = StateGraph(GraphState)
    entry = add_checked_node(builder, node, on_pass=END)
    builder.add_edge(START, entry)
    graph = builder.compile(checkpointer=InMemorySaver())
    graph.invoke(
        {},
        {
            "configurable": {
                "thread_id": "t",
                "run_id": run_id,
                "step_recorder": lambda step: None,
            }
        },
    )

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [(line["node"], line["attempt"], line["run_id"]) for line in lines] == [
        (KEY, 1, run_id),
        (KEY, 2, run_id),
    ]
    assert node_ctx.get() is None and attempt_ctx.get() is None
    assert run_id_ctx.get() is None  # restored after the attempt


# --------------------------------------------------------------------------
# CheckResult / Issue / build_feedback
# --------------------------------------------------------------------------


def test_an_error_issue_forces_ok_to_false() -> None:
    result = CheckResult(
        ok=True, issues=[Issue(code="x", message="m", severity="error")]
    )

    assert result.ok is False


def test_warnings_alone_keep_ok_true() -> None:
    result = CheckResult(
        ok=True, issues=[Issue(code="x", message="m", severity="warning")]
    )

    assert result.ok is True


def test_ok_false_stays_false_without_issues() -> None:
    assert CheckResult(ok=False).ok is False


def test_issue_message_and_feedback_are_clipped() -> None:
    issue = Issue(code="x", message="a" * 500, severity="error")
    result = CheckResult(ok=False, feedback="b" * 900)

    assert len(issue.message) == checked.MAX_ISSUE_MESSAGE_CHARS
    assert issue.message.endswith("…")
    assert len(result.feedback) == checked.MAX_FEEDBACK_CHARS


def test_short_text_is_not_clipped() -> None:
    assert Issue(code="x", message="  hello  ", severity="error").message == "hello"


@pytest.mark.parametrize("score", [-0.01, 1.01])
def test_score_must_be_between_zero_and_one(score: float) -> None:
    with pytest.raises(PydanticValidationError):
        CheckResult(ok=True, score=score)


def test_severity_must_be_error_or_warning() -> None:
    with pytest.raises(PydanticValidationError):
        Issue(code="x", message="m", severity="fatal")  # type: ignore[arg-type]


def test_check_result_matches_the_documented_fields() -> None:
    assert set(CheckResult.model_fields) == {"ok", "score", "issues", "feedback"}
    assert set(Issue.model_fields) == {"code", "message", "severity"}


def test_from_issues_builds_ok_and_feedback() -> None:
    errors = [
        Issue(code="a", message="Fix a.", severity="error"),
        Issue(code="b", message="Fix b.", severity="error"),
        Issue(code="w", message="Minor.", severity="warning"),
    ]

    failed = CheckResult.from_issues(errors, keep="the hook")
    passed = CheckResult.from_issues([errors[2]], score=0.9)

    assert failed.ok is False
    assert failed.feedback == "Fix a. Fix b. Keep: the hook"
    assert passed.ok is True
    assert passed.feedback == ""
    assert passed.score == 0.9
    assert CheckResult.from_issues().ok is True


def test_build_feedback_ignores_warnings_and_respects_the_limit() -> None:
    issues = [
        Issue(code="w", message="Just a warning.", severity="warning"),
        Issue(code="e", message="e" * 200, severity="error"),
        Issue(code="e", message="f" * 200, severity="error"),
        Issue(code="e", message="g" * 200, severity="error"),
    ]

    feedback = build_feedback(issues, keep="k" * 400)

    assert "warning" not in feedback
    assert len(feedback) <= checked.MAX_FEEDBACK_CHARS
    assert "Keep: " in feedback
    assert build_feedback([]) == ""
    assert build_feedback(issues[:1]) == ""
    assert build_feedback(issues[1:2], keep="  ") == "e" * 200


# --------------------------------------------------------------------------
# LangGraph control flow is not an error
# --------------------------------------------------------------------------


def test_an_interrupt_inside_the_agent_pauses_without_an_error_step() -> None:
    def asks(state: GraphState) -> dict[str, Any]:
        answer = interrupt("need input")
        return {"drafts": {"facebook": {"caption": answer}}}

    check, _ = scripted_check(ok())
    h = Harness(asks, check)

    paused = h.run()
    done = h.resume("typed by a human")

    assert paused["__interrupt__"][0].value == "need input"
    assert done["drafts"]["facebook"]["caption"] == "typed by a human"
    assert h.verdicts == ["passed"]  # the pause was not recorded as an error


def test_an_interrupt_inside_the_check_pauses_without_an_error_step() -> None:
    calls: list[int] = []

    def check(state: GraphState) -> CheckResult:
        calls.append(1)
        interrupt("confirm")
        return ok()

    h = Harness(Agent(), check)

    paused = h.run()
    done = h.resume("yes")

    assert paused["__interrupt__"][0].value == "confirm"
    assert done["status"] == "done"
    assert h.verdicts == ["passed"]
