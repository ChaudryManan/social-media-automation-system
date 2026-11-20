"""``researcher``: gathers sourced facts for the plan (``AGENT_SPECS.md`` 3.3).

Process: the LLM proposes up to ``max_queries`` search queries
(``researcher_queries``), the :class:`~app.providers.search.SearchProvider`
runs them, and the LLM summarises the results into :class:`ResearchNotes`
(``researcher_summary``). The notes are stored in ``state["research"]``.

If there are no results, or the provider fails, the node continues with
``grounded=false`` and a caution to avoid specific statistics (no summary call
is made without sources). A *configuration* problem (unknown provider, missing
or rejected API key) is not hidden: it is non-retriable and escalates.

The step is a checked node (``checked.make_checked_node``). The judge is
skipped when ``grounded`` is false.
"""

import json
from collections.abc import Callable
from typing import Literal

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
from app.agents.prompts import load_prompt
from app.agents.state import GraphState
from app.providers.search import (
    DEFAULT_MAX_RESULTS,
    SearchProvider,
    SearchProviderError,
    SearchResult,
    get_search_provider,
)

STEP_KEY = "researcher"
DEFAULT_MAX_QUERIES = 5
MAX_FACTS = 15
RESEARCH_TEMPERATURE = 0.2  # AGENT_SPECS 1.4: researcher is factual
NO_VALUE = "none"
UNGROUNDED_CAUTION = (
    "No usable sources were found: avoid specific statistics, dates and quotes."
)

RESEARCH_RUBRIC = """\
Score from 0 to 1 on these three points, equally weighted:
1. Relevance: the facts, trends and angles are about the topic and useful for the plan.
2. Source quality: facts rest on named, credible sources; weak or single sources
   are marked with lower confidence and a caution.
3. No hallucinated specifics: no number, date or quote without a source.
Report an error issue for an invented specific, a fact that does not belong to the
topic, or a source that looks made up."""


class SearchQueries(BaseModel):
    """Output of the ``researcher_queries`` prompt."""

    model_config = ConfigDict(str_strip_whitespace=True)

    queries: list[str]  # at most max_queries, most useful first


class Fact(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    claim: str
    source_url: str | None = None
    source_title: str | None = None
    confidence: Literal["high", "medium", "low"]


class ResearchNotes(BaseModel):
    """The researcher's output (``AGENT_SPECS.md`` 3.3).

    Counts and source rules are rule checks (retry feedback), not schema errors.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    facts: list[Fact]
    trends: list[str] = Field(default_factory=list)
    angles: list[str] = Field(default_factory=list)  # extra angle ideas
    cautions: list[str] = Field(default_factory=list)  # outdated/uncertain points
    grounded: bool  # False if no usable sources


def ungrounded_notes() -> ResearchNotes:
    return ResearchNotes(facts=[], cautions=[UNGROUNDED_CAUTION], grounded=False)


def _plan_text(state: GraphState) -> str:
    plan = state.get("plan")
    if not plan:
        return NO_VALUE
    return json.dumps(plan, ensure_ascii=False, sort_keys=True)


def clean_queries(queries: list[str], max_queries: int) -> list[str]:
    """Trim, drop empty and repeated queries (case-insensitive), keep the order."""
    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in queries:
        query = " ".join(str(raw).split())
        key = query.casefold()
        if not query or key in seen:
            continue
        seen.add(key)
        cleaned.append(query)
    return cleaned[:max_queries]


def format_results(results: list[SearchResult]) -> str:
    """Numbered blocks for the summary prompt; ``none`` when empty."""
    if not results:
        return NO_VALUE
    return "\n\n".join(
        f"[{n}] {r.title}\nURL: {r.url}\nSnippet: {r.snippet}"
        for n, r in enumerate(results, start=1)
    )


def _search_all(
    provider: SearchProvider, queries: list[str]
) -> tuple[list[SearchResult], int]:
    """Run every query; returns unique results (by URL) and the failure count.

    A failed query is skipped (the others still count). A configuration error
    is not a ``SearchProviderError`` and propagates.
    """
    results: list[SearchResult] = []
    seen: set[str] = set()
    failures = 0
    for query in queries:
        try:
            hits = provider.search(query)
        except SearchProviderError:
            failures += 1
            continue
        for hit in hits:
            if hit.url not in seen:
                seen.add(hit.url)
                results.append(hit)
    return results, failures


def make_research_agent(
    *,
    max_queries: int = DEFAULT_MAX_QUERIES,
    search_provider: Callable[[], SearchProvider] | None = None,
) -> Callable[[GraphState], AgentOutput]:
    """Build the agent function (queries -> search -> summary)."""
    if max_queries < 1:
        raise ValueError("max_queries must be at least 1")

    def get_provider() -> SearchProvider:
        if search_provider is not None:
            return search_provider()
        return get_search_provider(DEFAULT_MAX_RESULTS)

    def research_agent(state: GraphState) -> AgentOutput:
        feedback = (state.get("feedback") or {}).get(STEP_KEY)
        plan = _plan_text(state)

        queries_template = load_prompt("researcher_queries")
        proposed = generate_structured(
            SearchQueries,
            queries_template.messages(
                topic=state["topic"],
                language=state["language"],
                plan=plan,
                max_queries=str(max_queries),
            ),
            temperature=RESEARCH_TEMPERATURE,
        )
        queries = clean_queries(proposed.queries, max_queries)

        results, failures = _search_all(get_provider(), queries)
        summary_info = {
            "queries": len(queries),
            "results": len(results),
            "search_failures": failures,
        }

        if not results:
            notes = ungrounded_notes()
            return AgentOutput(
                update={"research": notes.model_dump()},
                prompt_version=queries_template.version,
                summary={**summary_info, "facts": 0, "grounded": False},
            )

        summary_template = load_prompt("researcher_summary")
        notes = generate_structured(
            ResearchNotes,
            summary_template.messages(
                feedback=feedback,
                topic=state["topic"],
                language=state["language"],
                plan=plan,
                search_results=format_results(results),
            ),
            temperature=RESEARCH_TEMPERATURE,
        )
        return AgentOutput(
            update={"research": notes.model_dump()},
            prompt_version=summary_template.version,
            summary={
                **summary_info,
                "facts": len(notes.facts),
                "grounded": notes.grounded,
            },
        )

    return research_agent


def _error(code: str, message: str) -> Issue:
    return Issue(code=code, message=message, severity="error")


def _claim_key(claim: str) -> str:
    return " ".join(claim.casefold().split())


def check_research(state: GraphState) -> CheckResult:
    """Rule checks (AGENT_SPECS 3.3): every high/medium fact has a ``source_url``,
    at most 15 facts, no duplicate claims. Also: no empty claims, source URLs are
    http(s), and ungrounded notes carry a caution."""
    notes = state.get("research") or {}
    facts = notes.get("facts") or []
    issues: list[Issue] = []

    if len(facts) > MAX_FACTS:
        issues.append(
            _error(
                "too_many_facts",
                f"Keep at most {MAX_FACTS} facts; there are {len(facts)}. "
                "Merge or drop the weakest.",
            )
        )

    seen: set[str] = set()
    duplicates: list[int] = []
    unsourced: list[int] = []
    bad_urls: list[int] = []
    empty: list[int] = []
    for number, fact in enumerate(facts, start=1):
        claim = str(fact.get("claim") or "")
        key = _claim_key(claim)
        if not key:
            empty.append(number)
        elif key in seen:
            duplicates.append(number)
        else:
            seen.add(key)
        url = str(fact.get("source_url") or "").strip()
        if fact.get("confidence") in ("high", "medium") and not url:
            unsourced.append(number)
        if url and not url.startswith(("http://", "https://")):
            bad_urls.append(number)

    if empty:
        issues.append(
            _error("claim_empty", f"Remove empty claims (facts {_numbers(empty)}).")
        )
    if duplicates:
        issues.append(
            _error(
                "duplicate_claims",
                f"Merge duplicate claims (facts {_numbers(duplicates)}).",
            )
        )
    if unsourced:
        issues.append(
            _error(
                "fact_source_missing",
                "Give a source_url from the search results to facts "
                f"{_numbers(unsourced)}, or mark them low confidence.",
            )
        )
    if bad_urls:
        issues.append(
            _error(
                "source_url_invalid",
                f"source_url must be a full http(s) link (facts {_numbers(bad_urls)}).",
            )
        )
    if not notes.get("grounded") and not [
        c for c in notes.get("cautions") or [] if str(c).strip()
    ]:
        issues.append(
            _error(
                "cautions_missing",
                "With grounded=false, add a caution to avoid specific statistics.",
            )
        )
    return CheckResult.from_issues(issues)


def _numbers(items: list[int]) -> str:
    return ", ".join(str(n) for n in items)


def make_research_judge() -> Callable[[GraphState], CheckResult]:
    """LLM judge for the notes (rubric: AGENT_SPECS 3.3). Skipped when ungrounded."""

    def judge(state: GraphState) -> CheckResult:
        notes = state.get("research") or {}
        if not notes.get("grounded"):
            return CheckResult(ok=True)
        messages = load_prompt("judge").messages(
            agent=STEP_KEY,
            language=state["language"],
            brand_voice=state.get("brand_voice"),
            rubric=RESEARCH_RUBRIC,
            platform_rules=NO_VALUE,
            plan=_plan_text(state),
            strategy=NO_VALUE,
            output=json.dumps(notes, ensure_ascii=False, sort_keys=True),
        )
        return generate_structured(CheckResult, messages, kind="fast", temperature=0.0)

    return judge


def make_researcher_node(
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    judge_threshold: float = DEFAULT_JUDGE_PASS_THRESHOLD,
    use_judge: bool = True,
    max_queries: int = DEFAULT_MAX_QUERIES,
    search_provider: Callable[[], SearchProvider] | None = None,
) -> CheckedNode:
    """The researcher as a checked node. Limits come from ``system_settings``.

    Wire it with ``checked.add_checked_node``.
    """
    return make_checked_node(
        STEP_KEY,
        make_research_agent(max_queries=max_queries, search_provider=search_provider),
        check_research,
        max_retries,
        judge_fn=make_research_judge() if use_judge else None,
        judge_threshold=judge_threshold,
    )
