"""The post subgraph: ``writer -> image -> hashtag`` (``ARCHITECTURE.md`` 6.1, 6.14).

Each step is a checked node (retry loop and human escalation, section 6.3) that
handles every distinct target platform in one attempt and writes
``drafts[platform]``. The compiled graph is meant to be passed to
``build_graph(post_subgraph=...)``, which uses it as the ``post_subgraph`` node; it
inherits the parent's checkpointer, so an escalation inside it pauses and resumes
with the rest of the run.

Re-entry from the reviewer follows ``rerun_from`` (AGENT_SPECS 5.2):

=============  ==============================================================
``rerun_from``  steps that run
=============  ==============================================================
none / other    writer, image, hashtag (first pass, or a re-entry above)
``writer``      writer, hashtag (and image only if the reviewer flagged it)
``image``       image only
``hashtag``     hashtag only
=============  ==============================================================

"Flagged" means ``feedback["image"]`` is set: a passed step clears its feedback and
the review gate sets it for every step named in an error. An aborted escalation
ends the subgraph with ``status="failed"``.
"""

from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agents.checked import CheckedNode, add_checked_node
from app.agents.post.hashtag import make_hashtag_node
from app.agents.post.image import make_image_node
from app.agents.post.writer import make_writer_node
from app.agents.state import GraphState

NODE_AFTER_WRITER = "after_writer"
NODE_AFTER_IMAGE = "after_image"


def _pass_through(state: GraphState) -> dict[str, Any]:
    return {}


def route_post_entry(state: GraphState) -> Literal["writer", "image", "hashtag"]:
    """The first step to run: the one the reviewer sent the flow back to."""
    start = state.get("rerun_from")
    return start if start in ("image", "hashtag") else "writer"  # type: ignore[return-value]


def route_after_writer(state: GraphState) -> Literal["image", "hashtag"]:
    """After a writer re-entry the image is kept unless the reviewer flagged it."""
    if state.get("rerun_from") == "writer" and not (state.get("feedback") or {}).get(
        "image"
    ):
        return "hashtag"
    return "image"


def route_after_image(state: GraphState) -> Literal["hashtag", "end"]:
    """An image-only re-entry stops after the image."""
    return "end" if state.get("rerun_from") == "image" else "hashtag"


def make_post_subgraph(
    *,
    writer: CheckedNode | None = None,
    image: CheckedNode | None = None,
    hashtag: CheckedNode | None = None,
) -> CompiledStateGraph:
    """Build the subgraph. Each part can be replaced (the worker passes nodes built
    with the limits from ``system_settings``, B3); the defaults use the builders'
    own defaults."""
    builder = StateGraph(GraphState)
    builder.add_node(NODE_AFTER_WRITER, _pass_through)
    builder.add_node(NODE_AFTER_IMAGE, _pass_through)
    # Wired from the end backwards: each checked node needs its successor's name.
    hashtag_entry = add_checked_node(
        builder, hashtag or make_hashtag_node(), on_pass=END
    )
    image_entry = add_checked_node(
        builder, image or make_image_node(), on_pass=NODE_AFTER_IMAGE
    )
    writer_entry = add_checked_node(
        builder, writer or make_writer_node(), on_pass=NODE_AFTER_WRITER
    )
    builder.add_conditional_edges(
        START,
        route_post_entry,
        {"writer": writer_entry, "image": image_entry, "hashtag": hashtag_entry},
    )
    builder.add_conditional_edges(
        NODE_AFTER_WRITER,
        route_after_writer,
        {"image": image_entry, "hashtag": hashtag_entry},
    )
    builder.add_conditional_edges(
        NODE_AFTER_IMAGE,
        route_after_image,
        {"hashtag": hashtag_entry, "end": END},
    )
    return builder.compile(name="post_subgraph")
