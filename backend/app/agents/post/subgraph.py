"""The post subgraph: ``writer -> image -> hashtag`` (``ARCHITECTURE.md`` 6.1).

Each step is a checked node (retry loop and human escalation, section 6.3) that
handles every distinct target platform in one attempt and writes
``drafts[platform]``. The compiled graph is meant to be passed to
``build_graph(post_subgraph=...)``, which uses it as the ``post_subgraph`` node; it
inherits the parent's checkpointer, so an escalation inside it pauses and resumes
with the rest of the run.

Re-entry from the reviewer (``rerun_from``, AGENT_SPECS 5.2) is not handled here:
the three nodes are separate, so B2-11 can wire partial reruns. An aborted
escalation ends the subgraph with ``status="failed"``.
"""

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agents.checked import CheckedNode, add_checked_node
from app.agents.post.hashtag import make_hashtag_node
from app.agents.post.image import make_image_node
from app.agents.post.writer import make_writer_node
from app.agents.state import GraphState


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
    # Wired from the end backwards: each checked node needs its successor's name.
    hashtag_entry = add_checked_node(
        builder, hashtag or make_hashtag_node(), on_pass=END
    )
    image_entry = add_checked_node(
        builder, image or make_image_node(), on_pass=hashtag_entry
    )
    writer_entry = add_checked_node(
        builder, writer or make_writer_node(), on_pass=image_entry
    )
    builder.add_edge(START, writer_entry)
    return builder.compile(name="post_subgraph")
