"""Tests for ``app.agents.state`` (B2-03)."""

import ast
import json
import re
from pathlib import Path
from typing import get_args, get_type_hints

import pytest
from app.agents import state
from app.agents.state import (
    CONTENT_TYPES,
    PLATFORMS,
    ContentType,
    GraphState,
    Platform,
    PlatformDraft,
    TargetAccount,
)

ARCHITECTURE = Path(__file__).resolve().parents[2] / "ARCHITECTURE.md"

EXPECTED_STATE_FIELDS = {
    "request_id",
    "workspace_id",
    "user_id",
    "topic",
    "content_type",
    "target_accounts",
    "publish_at",
    "brand_voice",
    "language",
    "plan",
    "research",
    "strategy",
    "drafts",
    "retries",
    "feedback",
    "attempt_info",
    "review",
    "review_cycles",
    "rerun_from",
    "approval",
    "publish_results",
    "status",
    "errors",
}
EXPECTED_DRAFT_FIELDS = {
    "caption",
    "hashtags",
    "title",
    "media",
    "script",
    "storyboard",
    "scenes",
}
SECRET_WORDS = ("token", "secret", "password", "credential", "api_key", "apikey")


def test_graph_state_has_the_documented_fields() -> None:
    assert set(GraphState.__annotations__) == EXPECTED_STATE_FIELDS


def test_platform_draft_has_the_documented_fields() -> None:
    assert set(PlatformDraft.__annotations__) == EXPECTED_DRAFT_FIELDS


def test_target_account_requires_both_fields() -> None:
    assert TargetAccount.__required_keys__ == {"account_id", "platform"}


def test_state_and_draft_fields_are_all_optional() -> None:
    # nodes return partial updates, and a run starts with only some fields set
    assert GraphState.__required_keys__ == frozenset()
    assert PlatformDraft.__required_keys__ == frozenset()


def test_platforms_and_content_types() -> None:
    assert PLATFORMS == ("facebook", "instagram", "tiktok", "youtube")
    assert CONTENT_TYPES == ("post", "video")
    assert get_args(Platform) == PLATFORMS
    assert get_args(ContentType) == CONTENT_TYPES


def test_field_types_follow_the_architecture_contract() -> None:
    hints = get_type_hints(GraphState)
    assert hints["content_type"] == ContentType
    assert hints["target_accounts"] == list[TargetAccount]
    assert hints["drafts"] == dict[str, PlatformDraft]
    assert hints["retries"] == dict[str, int]
    assert hints["feedback"] == dict[str, str | None]
    assert hints["review_cycles"] is int
    assert hints["errors"] == list[str]
    assert get_type_hints(TargetAccount)["platform"] == Platform


@pytest.mark.parametrize("typed_dict", [GraphState, PlatformDraft, TargetAccount])
def test_state_never_has_secret_looking_fields(typed_dict: type) -> None:
    offenders = [
        name
        for name in typed_dict.__annotations__
        if any(word in name.lower() for word in SECRET_WORDS)
    ]
    assert offenders == []


def _architecture_state_block() -> str:
    text = ARCHITECTURE.read_text(encoding="utf-8")
    section = text.split("### 6.2 State", 1)[1].split("### 6.3", 1)[0]
    match = re.search(r"```python\n(.*?)```", section, re.DOTALL)
    assert match, "ARCHITECTURE.md 6.2 has no python block"
    return match.group(1)


def _fields_by_class(source: str) -> dict[str, set[str]]:
    tree = ast.parse(source)
    return {
        node.name: {
            item.target.id
            for item in node.body
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
        }
        for node in tree.body
        if isinstance(node, ast.ClassDef)
    }


def test_code_matches_the_architecture_document() -> None:
    documented = _fields_by_class(_architecture_state_block())
    assert set(documented) == {"TargetAccount", "PlatformDraft", "GraphState"}
    for name, fields in documented.items():
        assert set(getattr(state, name).__annotations__) == fields, name


# --------------------------------------------------------------------------
# LangGraph accepts the state and can checkpoint it
# --------------------------------------------------------------------------


def _example_state() -> GraphState:
    return {
        "request_id": "11111111-1111-1111-1111-111111111111",
        "workspace_id": "22222222-2222-2222-2222-222222222222",
        "user_id": "33333333-3333-3333-3333-333333333333",
        "topic": "Building habits",
        "content_type": "post",
        "target_accounts": [{"account_id": "acc-1", "platform": "instagram"}],
        "publish_at": "2026-12-01T10:00:00+00:00",
        "brand_voice": None,
        "language": "en",
    }


def test_langgraph_runs_and_checkpoints_graph_state() -> None:
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph

    def planner(current: GraphState) -> GraphState:
        return {"plan": {"angle": "x"}, "retries": {"planner": 0}}

    def writer(current: GraphState) -> GraphState:
        draft: PlatformDraft = {"caption": "Hello", "hashtags": ["#a"]}
        return {
            "drafts": {"instagram": draft},
            "feedback": {"writer": None},
            "errors": [],
            "review_cycles": 0,
        }

    builder = StateGraph(GraphState)
    builder.add_node("planner", planner)
    builder.add_node("writer", writer)
    builder.add_edge(START, "planner")
    builder.add_edge("planner", "writer")
    builder.add_edge("writer", END)
    graph = builder.compile(checkpointer=MemorySaver())

    config = {"configurable": {"thread_id": "t-1"}}
    result = graph.invoke(_example_state(), config)

    assert result["topic"] == "Building habits"  # input kept
    assert result["plan"] == {"angle": "x"}
    assert result["drafts"]["instagram"]["caption"] == "Hello"
    assert result["feedback"] == {"writer": None}

    saved = graph.get_state(config).values  # what a checkpoint stores
    assert saved["drafts"] == result["drafts"]
    json.dumps(saved)  # the whole state stays JSON-serialisable
