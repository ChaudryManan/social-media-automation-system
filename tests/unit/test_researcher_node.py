"""Tests for the ``researcher`` node (B2-06): fake LLM plus ``FakeSearch``."""

import json
import re
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from app.agents.checked import StepRecord, add_checked_node
from app.agents.llm import FakeScript
from app.agents.nodes.researcher import (
    MAX_FACTS,
    RESEARCH_RUBRIC,
    UNGROUNDED_CAUTION,
    Fact,
    ResearchNotes,
    SearchQueries,
    check_research,
    clean_queries,
    format_results,
    make_research_agent,
    make_research_judge,
    make_researcher_node,
)
from app.agents.prompts import load_prompt
from app.agents.state import GraphState
from app.core.config import get_settings
from app.providers.search import (
    FakeSearch,
    SearchConfigError,
    SearchResult,
    get_fake_search,
)
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "AGENT_SPECS.md"
PROMPTS = ROOT / "backend" / "app" / "agents" / "prompts"


def fact(n: int = 1, **changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "claim": f"Claim number {n}",
        "source_url": f"https://example.com/{n}",
        "source_title": f"Source {n}",
        "confidence": "high",
    }
    data.update(changes)
    return data


def notes(**changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "facts": [fact(1), fact(2, confidence="medium")],
        "trends": ["More people drink tea at work"],
        "angles": ["Tea as a focus ritual"],
        "cautions": ["Health effects vary between studies"],
        "grounded": True,
    }
    data.update(changes)
    return data


def queries(*items: str) -> dict[str, Any]:
    return {"queries": list(items) or ["green tea benefits", "tea trends"]}


def good_judge(score: float = 0.9) -> dict[str, Any]:
    return {"ok": True, "score": score, "issues": [], "feedback": ""}


def hit(n: int = 1) -> SearchResult:
    return SearchResult(f"Title {n}", f"https://example.com/{n}", f"Snippet {n}")


def state(**changes: Any) -> GraphState:
    base: dict[str, Any] = {
        "request_id": "r1",
        "workspace_id": "w1",
        "user_id": "u1",
        "topic": "Benefits of green tea",
        "content_type": "post",
        "target_accounts": [{"account_id": "a1", "platform": "instagram"}],
        "publish_at": "2026-12-01T10:00:00Z",
        "brand_voice": "warm and simple",
        "language": "en",
        "plan": {"angle": "Tea is a daily habit", "key_messages": ["Easy start"]},
    }
    base.update(changes)
    return base  # type: ignore[return-value]


@pytest.fixture
def search() -> Iterator[FakeSearch]:
    yield FakeSearch()


# --------------------------------------------------------------------------
# Models and the spec / prompt contract
# --------------------------------------------------------------------------


def spec_fields(class_name: str) -> set[str]:
    text = SPEC.read_text(encoding="utf-8")
    block = re.search(
        rf"class {class_name}\(BaseModel\):\n(.*?)(?:\n\n|```)", text, re.S
    )
    assert block, f"{class_name} is missing from AGENT_SPECS.md"
    return set(re.findall(r"^    (\w+):", block.group(1), re.M))


def prompt_example(name: str) -> dict[str, Any]:
    text = (PROMPTS / f"{name}.md").read_text(encoding="utf-8")
    return json.loads(re.search(r"```json\n(.*?)```", text, re.S).group(1))  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("model", "name"),
    [
        (SearchQueries, "SearchQueries"),
        (Fact, "Fact"),
        (ResearchNotes, "ResearchNotes"),
    ],
)
def test_model_fields_equal_the_spec_classes(model: type, name: str) -> None:
    assert set(model.model_fields) == spec_fields(name)  # type: ignore[attr-defined]


def test_queries_prompt_example_fields_equal_the_model_fields() -> None:
    example = prompt_example("researcher_queries")

    assert set(example) == set(SearchQueries.model_fields)
    SearchQueries.model_validate(example)


def test_summary_prompt_example_fields_equal_the_model_fields() -> None:
    example = prompt_example("researcher_summary")

    assert set(example) == set(ResearchNotes.model_fields)
    assert set(example["facts"][0]) == set(Fact.model_fields)
    ResearchNotes.model_validate(example)


def test_a_full_note_round_trips() -> None:
    assert ResearchNotes.model_validate(notes()).model_dump() == notes()


def test_optional_parts_have_defaults() -> None:
    parsed = ResearchNotes.model_validate({"facts": [], "grounded": False})

    assert parsed.trends == [] and parsed.angles == [] and parsed.cautions == []
    assert Fact.model_validate({"claim": "c", "confidence": "low"}).source_url is None


@pytest.mark.parametrize("confidence", ["certain", "", None, "High"])
def test_an_unknown_confidence_is_a_schema_error(confidence: Any) -> None:
    with pytest.raises(ValidationError):
        Fact.model_validate(fact(confidence=confidence))


@pytest.mark.parametrize("field", ["facts", "grounded"])
def test_facts_and_grounded_are_required(field: str) -> None:
    data = notes()
    del data[field]

    with pytest.raises(ValidationError):
        ResearchNotes.model_validate(data)


def test_the_limits_are_rule_checks_not_schema_errors() -> None:
    parsed = ResearchNotes.model_validate(
        notes(facts=[fact(1, claim="", source_url=None)] * 20)
    )

    assert len(parsed.facts) == 20


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def test_clean_queries_trims_dedupes_case_insensitively_and_caps() -> None:
    raw = ["  tea   benefits ", "Tea benefits", "", "   ", "tea trends", "c", "d", "e"]

    assert clean_queries(raw, 3) == ["tea benefits", "tea trends", "c"]


def test_format_results_numbers_the_blocks_and_says_none_when_empty() -> None:
    assert format_results([]) == "none"
    assert format_results([hit(1), hit(2)]) == (
        "[1] Title 1\nURL: https://example.com/1\nSnippet: Snippet 1\n\n"
        "[2] Title 2\nURL: https://example.com/2\nSnippet: Snippet 2"
    )


def test_a_zero_query_limit_is_rejected() -> None:
    with pytest.raises(ValueError, match="max_queries"):
        make_research_agent(max_queries=0)


# --------------------------------------------------------------------------
# research_agent
# --------------------------------------------------------------------------


def test_the_agent_searches_every_query_and_stores_the_notes(
    fake_llm: FakeScript, search: FakeSearch
) -> None:
    fake_llm.queue(queries("q one", "q two"), notes())
    search.queue([hit(1), hit(2)], [hit(2), hit(3)])  # hit(2) comes back twice

    out = make_research_agent(search_provider=lambda: search)(state())

    assert out.update == {"research": notes()}
    assert search.calls == ["q one", "q two"]
    assert out.prompt_version == "researcher_summary.v1"
    assert out.summary == {
        "queries": 2,
        "results": 3,  # duplicates by URL are merged
        "search_failures": 0,
        "facts": 2,
        "grounded": True,
    }
    assert [c.kind for c in fake_llm.calls] == ["text", "text"]


def test_the_prompts_get_the_inputs_and_user_text_stays_out_of_system(
    fake_llm: FakeScript, search: FakeSearch
) -> None:
    fake_llm.queue(queries("q one"), notes())
    search.set_results([hit(1)])

    make_research_agent(max_queries=4, search_provider=lambda: search)(
        state(language="ur")
    )

    q_system, q_user = fake_llm.calls[0].messages
    s_system, s_user = fake_llm.calls[1].messages
    assert "Benefits of green tea" in q_user.content
    assert "Output language: ur" in q_user.content
    assert "At most 4 queries" in q_system.content
    assert "Tea is a daily habit" in q_user.content  # the plan
    assert "[1] Title 1\nURL: https://example.com/1" in s_user.content
    assert "Benefits of green tea" not in q_system.content + s_system.content
    assert "Snippet 1" not in s_system.content


def test_the_temperature_is_the_documented_factual_one(
    fake_llm: FakeScript, search: FakeSearch
) -> None:
    fake_llm.queue(queries("q"), notes())

    make_research_agent(search_provider=lambda: search)(state())

    assert [c.temperature for c in fake_llm.calls] == [0.2, 0.2]


def test_more_queries_than_the_limit_are_cut(
    fake_llm: FakeScript, search: FakeSearch
) -> None:
    fake_llm.queue(queries("a", "b", "c", "d", "e", "f", "g"), notes())

    make_research_agent(max_queries=2, search_provider=lambda: search)(state())

    assert search.calls == ["a", "b"]


def test_without_results_the_node_is_ungrounded_and_skips_the_summary(
    fake_llm: FakeScript, search: FakeSearch
) -> None:
    fake_llm.queue(queries("q one"))
    search.set_results([])

    out = make_research_agent(search_provider=lambda: search)(state())

    (note,) = [out.update["research"]]
    assert note == {
        "facts": [],
        "trends": [],
        "angles": [],
        "cautions": [UNGROUNDED_CAUTION],
        "grounded": False,
    }
    assert len(fake_llm.calls) == 1  # only the queries call
    assert out.summary["grounded"] is False
    assert "avoid specific statistics" in UNGROUNDED_CAUTION


def test_a_failed_provider_continues_ungrounded(
    fake_llm: FakeScript, search: FakeSearch
) -> None:
    fake_llm.queue(queries("q one", "q two"))
    search.fail_next(2)

    out = make_research_agent(search_provider=lambda: search)(state())

    assert out.update["research"]["grounded"] is False
    assert out.summary["search_failures"] == 2
    assert len(fake_llm.calls) == 1


def test_one_failed_query_does_not_hide_the_others(
    fake_llm: FakeScript, search: FakeSearch
) -> None:
    fake_llm.queue(queries("q one", "q two"), notes())
    search.fail_next(1)
    search.set_results([hit(1)])

    out = make_research_agent(search_provider=lambda: search)(state())

    assert out.summary["search_failures"] == 1 and out.summary["results"] == 1
    assert out.update["research"]["grounded"] is True


def test_a_config_error_is_not_swallowed(fake_llm: FakeScript) -> None:
    fake_llm.queue(queries("q"))

    def broken() -> FakeSearch:
        raise SearchConfigError("no key")

    with pytest.raises(SearchConfigError):
        make_research_agent(search_provider=broken)(state())


def test_the_default_provider_comes_from_the_registry(
    fake_llm: FakeScript, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SEARCH_PROVIDER", "fake")
    get_settings.cache_clear()
    shared = get_fake_search()
    shared.reset()
    fake_llm.queue(queries("q one"), notes())
    try:
        out = make_research_agent()(state())
        calls = list(shared.calls)
    finally:
        shared.reset()
        get_settings.cache_clear()

    assert out.update["research"]["grounded"] is True
    assert calls == ["q one"]


def test_a_missing_plan_is_sent_as_none(
    fake_llm: FakeScript, search: FakeSearch
) -> None:
    fake_llm.queue(queries("q"), notes())
    base = state()
    del base["plan"]  # type: ignore[misc]

    make_research_agent(search_provider=lambda: search)(base)

    assert "Content plan:\nnone" in fake_llm.calls[0].messages[1].content


def test_retry_feedback_reaches_the_summary_prompt_only(
    fake_llm: FakeScript, search: FakeSearch
) -> None:
    fake_llm.queue(queries("q"), notes())

    make_research_agent(search_provider=lambda: search)(
        state(feedback={"researcher": "Add a source_url to fact 2."})
    )

    assert "Previous attempt feedback" not in fake_llm.calls[0].messages[1].content
    assert "Add a source_url to fact 2." in fake_llm.calls[1].messages[1].content


# --------------------------------------------------------------------------
# Rule checks
# --------------------------------------------------------------------------


def checked(**changes: Any):  # type: ignore[no-untyped-def]
    return check_research(state(research=notes(**changes)))


def codes(result: Any) -> list[str]:
    return [i.code for i in result.issues]


def test_good_notes_pass() -> None:
    assert checked().ok is True


def test_fifteen_facts_pass_and_sixteen_fail() -> None:
    assert checked(facts=[fact(n) for n in range(MAX_FACTS)]).ok is True

    result = checked(facts=[fact(n) for n in range(MAX_FACTS + 1)])

    assert codes(result) == ["too_many_facts"]
    assert "16" in result.feedback


@pytest.mark.parametrize("confidence", ["high", "medium"])
@pytest.mark.parametrize("url", [None, "", "   "])
def test_high_and_medium_facts_need_a_source(confidence: str, url: Any) -> None:
    result = checked(facts=[fact(1), fact(2, source_url=url, confidence=confidence)])

    assert codes(result) == ["fact_source_missing"]
    assert "facts 2" in result.feedback


def test_a_low_confidence_fact_may_have_no_source() -> None:
    assert checked(facts=[fact(1, source_url=None, confidence="low")]).ok is True


@pytest.mark.parametrize("url", ["example.com/x", "ftp://x.com", "javascript:1"])
def test_a_source_must_be_an_http_link(url: str) -> None:
    result = checked(facts=[fact(1, source_url=url, confidence="low")])

    assert codes(result) == ["source_url_invalid"]


def test_duplicate_claims_are_found_ignoring_case_and_spacing() -> None:
    result = checked(
        facts=[fact(1, claim="Tea has  caffeine"), fact(2, claim="tea HAS caffeine")]
    )

    assert codes(result) == ["duplicate_claims"]
    assert "facts 2" in result.feedback


@pytest.mark.parametrize("claim", ["", "   ", None])
def test_empty_claims_fail(claim: Any) -> None:
    assert codes(checked(facts=[fact(1, claim=claim)])) == ["claim_empty"]


def test_ungrounded_notes_need_a_caution() -> None:
    assert codes(checked(facts=[], grounded=False, cautions=[])) == ["cautions_missing"]
    assert codes(checked(facts=[], grounded=False, cautions=["  "])) == [
        "cautions_missing"
    ]
    assert checked(facts=[], grounded=False, cautions=["avoid statistics"]).ok


def test_grounded_notes_need_no_caution() -> None:
    assert checked(cautions=[]).ok is True


def test_every_problem_is_reported_together() -> None:
    result = checked(facts=[fact(1, source_url=None), fact(2, claim="Claim number 1")])

    assert sorted(codes(result)) == ["duplicate_claims", "fact_source_missing"]


def test_missing_notes_fail_instead_of_crashing() -> None:
    result = check_research(state())

    assert codes(result) == ["cautions_missing"]


def test_the_rules_do_not_call_the_llm(fake_llm: FakeScript) -> None:
    checked()

    assert fake_llm.calls == []


# --------------------------------------------------------------------------
# Judge
# --------------------------------------------------------------------------


def test_the_judge_sends_notes_and_rubric_to_the_fast_model(
    fake_llm: FakeScript,
) -> None:
    fake_llm.queue(good_judge(0.8))

    verdict = make_research_judge()(state(research=notes()))

    assert verdict.ok and verdict.score == 0.8
    call = fake_llm.calls[0]
    assert call.kind == "fast" and call.temperature == 0.0
    user = call.messages[1].content
    assert RESEARCH_RUBRIC.splitlines()[0] in user
    assert "Claim number 1" in user and "Tea is a daily habit" in user
    assert "Step being judged: researcher" in user
    assert load_prompt("judge").version in str(load_prompt("judge").version)


def test_the_judge_is_skipped_when_ungrounded(fake_llm: FakeScript) -> None:
    verdict = make_research_judge()(state(research=notes(facts=[], grounded=False)))

    assert verdict.ok is True and verdict.score is None
    assert fake_llm.calls == []


# --------------------------------------------------------------------------
# The checked node (agent + rules + judge + retry)
# --------------------------------------------------------------------------


class Flow:
    """START -> researcher (checked) -> END."""

    def __init__(self, search: FakeSearch, **node_args: Any) -> None:
        self.steps: list[StepRecord] = []
        self.node = make_researcher_node(search_provider=lambda: search, **node_args)
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


def test_good_notes_pass_at_once(fake_llm: FakeScript, search: FakeSearch) -> None:
    fake_llm.queue(queries("q"), notes(), good_judge())
    flow = Flow(search)

    result = flow.run()

    assert result["research"] == notes()
    assert [c.kind for c in fake_llm.calls] == ["text", "text", "fast"]
    (step,) = flow.steps
    assert (step.node_name, step.attempt, step.verdict) == ("researcher", 1, "passed")
    assert step.output_summary["prompt_version"] == "researcher_summary.v1"
    assert step.output_summary["score"] == 0.9
    assert step.tokens_in > 0


def test_an_ungrounded_run_passes_without_a_summary_or_judge_call(
    fake_llm: FakeScript, search: FakeSearch
) -> None:
    fake_llm.queue(queries("q"))
    search.set_results([])
    flow = Flow(search)

    result = flow.run()

    assert result["research"]["grounded"] is False
    assert [c.kind for c in fake_llm.calls] == ["text"]
    assert [s.verdict for s in flow.steps] == ["passed"]


def test_a_rule_failure_retries_with_feedback_and_skips_the_judge(
    fake_llm: FakeScript, search: FakeSearch
) -> None:
    bad = notes(facts=[fact(1, source_url=None)])
    fake_llm.queue(queries("q"), bad, queries("q"), notes(), good_judge())
    flow = Flow(search)

    result = flow.run()

    assert result["research"] == notes()
    assert [c.kind for c in fake_llm.calls] == ["text"] * 4 + ["fast"]
    retry_summary_prompt = fake_llm.calls[3].messages[1].content
    assert "Give a source_url from the search results to facts 1" in (
        retry_summary_prompt
    )
    assert [s.verdict for s in flow.steps] == ["retried", "passed"]
    assert result["retries"] == {"researcher": 1}


def test_a_low_judge_score_retries_with_the_judge_feedback(
    fake_llm: FakeScript, search: FakeSearch
) -> None:
    low = {
        "ok": False,
        "score": 0.3,
        "issues": [
            {
                "code": "off_topic",
                "message": "Facts are off topic.",
                "severity": "error",
            }
        ],
        "feedback": "Facts are off topic. Keep: the sources",
    }
    fake_llm.queue(queries("q"), notes(), low, queries("q"), notes(), good_judge())
    flow = Flow(search)

    flow.run()

    assert (
        "Facts are off topic. Keep: the sources"
        in fake_llm.calls[4].messages[1].content
    )
    assert [s.verdict for s in flow.steps] == ["retried", "passed"]


def test_exhausted_retries_escalate_and_resume_retry_continues(
    fake_llm: FakeScript, search: FakeSearch
) -> None:
    bad = notes(facts=[fact(1), fact(2, claim="claim number 1")])
    fake_llm.queue(
        queries("q"), bad, queries("q"), bad, queries("q"), bad,
        queries("q"), notes(), good_judge(),
    )  # fmt: skip
    flow = Flow(search)

    paused = flow.run()

    (interrupt,) = paused["__interrupt__"]
    assert interrupt.value["step"] == "researcher"
    assert interrupt.value["reason"] == "retries_exhausted"
    assert [i["code"] for i in interrupt.value["issues"]] == ["duplicate_claims"]

    done = flow.resume({"action": "retry"})

    assert done["research"] == notes()


def test_a_config_error_escalates_at_once_as_non_retriable(
    fake_llm: FakeScript,
) -> None:
    fake_llm.queue(queries("q"))

    def broken() -> FakeSearch:
        raise SearchConfigError("TAVILY_API_KEY is not set")

    flow = Flow.__new__(Flow)
    flow.steps = []
    flow.node = make_researcher_node(search_provider=broken)
    builder = StateGraph(GraphState)
    entry = add_checked_node(builder, flow.node, on_pass=END)
    builder.add_edge(START, entry)
    flow.graph = builder.compile(checkpointer=InMemorySaver())
    flow.config = {
        "configurable": {"thread_id": "t", "step_recorder": flow.steps.append}
    }

    paused = flow.run()

    (interrupt,) = paused["__interrupt__"]
    assert interrupt.value["reason"] == "non_retriable_error"
    assert interrupt.value["error_code"] == "search_not_configured"
    assert [s.verdict for s in flow.steps] == ["escalated"]


def test_an_llm_outage_is_a_retried_error_attempt(
    fake_llm: FakeScript, search: FakeSearch
) -> None:
    fake_llm.fail_next(1)
    fake_llm.queue(queries("q"), notes(), good_judge())
    flow = Flow(search)

    result = flow.run()

    assert result["research"] == notes()
    assert [s.verdict for s in flow.steps] == ["error", "passed"]


def test_without_the_judge_only_the_rules_decide(
    fake_llm: FakeScript, search: FakeSearch
) -> None:
    fake_llm.queue(queries("q"), notes())

    result = Flow(search, use_judge=False).run()

    assert result["research"] == notes()
    assert [c.kind for c in fake_llm.calls] == ["text", "text"]


def test_the_node_uses_the_documented_step_key_and_default_limits() -> None:
    node = make_researcher_node()

    assert node.key == "researcher"
    assert node.max_retries == 3
