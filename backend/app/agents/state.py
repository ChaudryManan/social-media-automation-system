"""Graph state for the generation workflow (ARCHITECTURE.md 6.2).

``GraphState`` is the LangGraph state schema. It is checkpointed to the
database, so it must stay small and JSON-serialisable and must **never**
contain tokens, passwords or other secrets: publishers load credentials from
``social_accounts`` by ``account_id`` instead.

Values written by agents (``plan``, ``research``, ``strategy``, ``review``...)
are the ``model_dump()`` of the output schemas in ``AGENT_SPECS.md``; the
Pydantic models for them arrive with the nodes (B2-05 onwards).

Changing a field here means changing ``ARCHITECTURE.md`` 6.2 in the same
change (and logging it in ``PROGRESS.md``).
"""

from typing import Literal, TypedDict, get_args

Platform = Literal["facebook", "instagram", "tiktok", "youtube"]
ContentType = Literal["post", "video"]

PLATFORMS: tuple[str, ...] = get_args(Platform)
CONTENT_TYPES: tuple[str, ...] = get_args(ContentType)


class TargetAccount(TypedDict):
    """One account the content will be published to (never holds credentials)."""

    account_id: str
    platform: Platform


class PlatformDraft(TypedDict, total=False):
    """The generated content for one platform."""

    caption: str
    hashtags: list[str]
    title: str  # youtube / video
    media: list[dict]  # [{"type": "image|video|audio|subtitle", "storage_key": ...}]
    script: dict  # video only
    storyboard: list[dict]  # video only
    scenes: list[dict]  # video only


class GraphState(TypedDict, total=False):
    """State shared by every node of the main graph and its subgraphs."""

    # request context (set once when the run starts)
    request_id: str
    workspace_id: str
    user_id: str
    topic: str
    content_type: ContentType
    target_accounts: list[TargetAccount]
    publish_at: str  # ISO-8601 UTC
    brand_voice: str | None
    language: str

    # agent outputs
    plan: dict | None
    research: dict | None
    strategy: dict | None  # keyed by platform
    drafts: dict[str, PlatformDraft]  # keyed by platform

    # retry / review bookkeeping
    retries: dict[str, int]  # keyed by step key, e.g. "writer", "scene:2"
    feedback: dict[str, str | None]  # last checker feedback per step key
    review: dict | None
    review_cycles: int
    rerun_from: str | None

    # approval, publishing and run status
    approval: dict | None
    publish_results: list[dict]
    status: str
    errors: list[str]
