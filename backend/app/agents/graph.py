"""The main workflow graph (``ARCHITECTURE.md`` 6.1 and 6.5).

Today it covers the shared part of the flow::

    START -> validate_input -(ok)-> planner -> researcher -> strategist
          -> content_router -(post)-> post_subgraph -> reviewer -> review_gate
                            -(video)-> video_subgraph -> reviewer -> review_gate
          review_gate -(ok)-> END
                      -(fix)-> planner | strategist | content_router

``validate_input`` ends the run when the request is invalid. The three
generating steps are checked nodes (retry loop and escalation, section 6.3).
``post_subgraph`` and ``video_subgraph`` are **placeholders** that only write
``drafts = {}`` unless the real ones (B2-10, B4) are passed to :func:`build_graph`.
Their output goes to the checked ``reviewer`` and the ``review_gate`` node
(``nodes/reviewer.py``), which either ends the run (``ok``, or the cycle cap) or sends
it back to the step to redo; the approval step (B3) will follow ``ok``. A content
subgraph that ended with ``status="failed"`` (a human aborted a step) skips the review.

``content_router`` is a pass-through node with a conditional edge, so the
reviewer and the approval step can send the run back to it. It is a pure
function of ``content_type``. ``rerun_from`` is left untouched: the content
subgraphs read it to decide which of their nodes to run again.

Persistence: ``thread_id = request_id`` and the LangGraph Postgres saver
(``postgres_checkpointer``), whose tables are created by the library's
``setup()``, not by Alembic. The graph runs in a Celery worker, never in an
API request.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agents.checked import CheckedNode, add_checked_node
from app.agents.nodes.planner import make_planner_node
from app.agents.nodes.researcher import make_researcher_node
from app.agents.nodes.reviewer import (
    DEFAULT_MAX_REVIEW_CYCLES,
    make_review_gate,
    make_reviewer_node,
    route_after_content,
    route_after_review,
)
from app.agents.nodes.strategist import make_strategist_node
from app.agents.nodes.validate_input import (
    make_validate_input_node,
    route_after_validation,
)
from app.agents.state import GraphState
from app.core.config import get_settings

NODE_VALIDATE_INPUT = "validate_input"
NODE_CONTENT_ROUTER = "content_router"
NODE_POST_SUBGRAPH = "post_subgraph"
NODE_VIDEO_SUBGRAPH = "video_subgraph"
NODE_REVIEW_GATE = "review_gate"

Node = Callable[..., Any]


def content_router(state: GraphState) -> dict[str, Any]:
    """Pass-through node: the decision is made by :func:`route_content_type`."""
    return {}


def route_content_type(state: GraphState) -> Literal["post", "video"]:
    """``post`` or ``video`` from ``content_type`` (validated by ``validate_input``)."""
    content_type = state.get("content_type")
    if content_type not in ("post", "video"):
        # Not an AppError: a run that got here without validation is a bug.
        raise ValueError(
            f"content_type must be 'post' or 'video', got {content_type!r}"
        )
    return content_type  # type: ignore[return-value]


def post_placeholder(state: GraphState) -> dict[str, Any]:
    """Stand-in for the post subgraph (B2-10); writes no content."""
    return {"drafts": {}}


def video_placeholder(state: GraphState) -> dict[str, Any]:
    """Stand-in for the video subgraph (B4); writes no content."""
    return {"drafts": {}}


def build_graph(
    *,
    validate_input: Node | None = None,
    planner: CheckedNode | None = None,
    researcher: CheckedNode | None = None,
    strategist: CheckedNode | None = None,
    post_subgraph: Node | None = None,
    video_subgraph: Node | None = None,
    reviewer: CheckedNode | None = None,
    max_review_cycles: int = DEFAULT_MAX_REVIEW_CYCLES,
    with_review: bool = True,
) -> StateGraph:
    """Build the (uncompiled) main graph.

    Every part can be replaced; the defaults use each builder's own defaults
    (limits from ``system_settings`` are passed in by the worker, B3).
    ``post_subgraph`` and ``video_subgraph`` may be plain node functions or
    compiled subgraphs. ``with_review=False`` ends the run right after the content
    subgraph (no reviewer), which keeps graph tests of earlier steps simple.
    """
    builder = StateGraph(GraphState)

    builder.add_node(NODE_VALIDATE_INPUT, validate_input or make_validate_input_node())
    builder.add_node(NODE_CONTENT_ROUTER, content_router)
    builder.add_node(NODE_POST_SUBGRAPH, post_subgraph or post_placeholder)
    builder.add_node(NODE_VIDEO_SUBGRAPH, video_subgraph or video_placeholder)

    # Wired from the end backwards: each checked node needs its successor's name.
    reviewer_entry = None
    if with_review:
        builder.add_node(NODE_REVIEW_GATE, make_review_gate(max_review_cycles))
        reviewer_entry = add_checked_node(
            builder, reviewer or make_reviewer_node(), on_pass=NODE_REVIEW_GATE
        )
    strategist_entry = add_checked_node(
        builder, strategist or make_strategist_node(), on_pass=NODE_CONTENT_ROUTER
    )
    researcher_entry = add_checked_node(
        builder, researcher or make_researcher_node(), on_pass=strategist_entry
    )
    planner_entry = add_checked_node(
        builder, planner or make_planner_node(), on_pass=researcher_entry
    )

    builder.add_edge(START, NODE_VALIDATE_INPUT)
    builder.add_conditional_edges(
        NODE_VALIDATE_INPUT,
        route_after_validation,
        {"ok": planner_entry, "failed": END},
    )
    builder.add_conditional_edges(
        NODE_CONTENT_ROUTER,
        route_content_type,
        {"post": NODE_POST_SUBGRAPH, "video": NODE_VIDEO_SUBGRAPH},
    )
    if reviewer_entry is None:
        builder.add_edge(NODE_POST_SUBGRAPH, END)
        builder.add_edge(NODE_VIDEO_SUBGRAPH, END)
    else:
        for content_node in (NODE_POST_SUBGRAPH, NODE_VIDEO_SUBGRAPH):
            builder.add_conditional_edges(
                content_node,
                route_after_content,
                {"review": reviewer_entry, "failed": END},
            )
        builder.add_conditional_edges(
            NODE_REVIEW_GATE,
            route_after_review,
            {
                "ok": END,
                "planner": planner_entry,
                "strategist": strategist_entry,
                "content": NODE_CONTENT_ROUTER,
            },
        )
    return builder


def compile_graph(
    checkpointer: BaseCheckpointSaver | None, **parts: Any
) -> CompiledStateGraph:
    """Build the main graph and compile it with ``checkpointer``.

    Without a checkpointer a run cannot be resumed after an interrupt (the
    escalation node needs one), so pass one outside of simple tests.
    """
    return build_graph(**parts).compile(checkpointer=checkpointer)


def to_psycopg_url(database_url: str) -> str:
    """``postgresql+psycopg://...`` (SQLAlchemy) -> ``postgresql://...`` (psycopg)."""
    scheme, sep, rest = database_url.partition("://")
    if not sep:
        raise ValueError("database_url must look like postgresql://user:pass@host/db")
    return f"{scheme.split('+', 1)[0]}://{rest}"


@contextmanager
def postgres_checkpointer(
    database_url: str | None = None, *, setup: bool = True
) -> Iterator[PostgresSaver]:
    """A LangGraph Postgres saver on ``database_url`` (default ``DATABASE_URL``).

    ``setup=True`` creates or migrates the checkpoint tables (idempotent). The
    connection is closed when the block ends; a worker that runs many
    requests opens it once and reuses the saver.
    """
    url = to_psycopg_url(database_url or get_settings().DATABASE_URL)
    with PostgresSaver.from_conn_string(url) as saver:
        if setup:
            saver.setup()
        yield saver


def thread_config(request_id: str, **configurable: Any) -> dict[str, Any]:
    """The run config: ``thread_id = request_id`` plus extras (``run_id``, ...)."""
    return {"configurable": {"thread_id": request_id, **configurable}}
