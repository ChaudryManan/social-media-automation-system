"""Dev runner: run the agent graph from the command line (B2-12).

    python -m app.agents.run --topic "Benefits of green tea" --type post \\
        --platforms facebook,instagram

Builds the main graph with the real post subgraph, the reviewer and an in-memory
checkpointer, runs it until it ends or the first interrupt (a human escalation) and
prints the drafts and the review. Providers come from the settings (``.env``):
with ``LLM_PROVIDER=fake`` (the default) the runner scripts the fake LLM itself, so
it works offline; with a real provider it calls that provider. Images are stored by
the configured storage backend (``--storage-dir`` overrides the directory).

Exit codes: 0 finished, 1 the request was rejected or the run failed, 2 paused for
a human (the interrupt is printed).
"""

import argparse
import json
import os
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from app.agents.checked import StepRecord
from app.agents.fake_flow import post_flow_replies
from app.agents.graph import compile_graph, thread_config
from app.agents.llm import get_fake_script
from app.agents.nodes.reviewer import DEFAULT_MAX_REVIEW_CYCLES
from app.agents.post import make_post_subgraph
from app.agents.state import GraphState
from app.core.config import get_settings

EXIT_OK, EXIT_FAILED, EXIT_PAUSED = 0, 1, 2


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.agents.run",
        description="Run the content graph and print the draft.",
    )
    parser.add_argument("--topic", required=True, help="what the content is about")
    parser.add_argument(
        "--type",
        dest="content_type",
        choices=["post"],
        default="post",
        help="content type (video arrives with B4)",
    )
    parser.add_argument(
        "--platforms",
        default="facebook,instagram",
        help="comma separated: facebook,instagram,...",
    )
    parser.add_argument("--language", default="en", help="output language code")
    parser.add_argument("--brand-voice", default=None, help="brand voice text")
    parser.add_argument("--storage-dir", default=None, help="where images are stored")
    parser.add_argument(
        "--max-review-cycles", type=int, default=DEFAULT_MAX_REVIEW_CYCLES
    )
    parser.add_argument("--json", action="store_true", help="print JSON, not text")
    return parser.parse_args(argv)


def build_state(args: argparse.Namespace) -> GraphState:
    """The request as the worker would hand it to the graph."""
    platforms = [p.strip().casefold() for p in args.platforms.split(",") if p.strip()]
    publish_at = datetime.now(UTC) + timedelta(days=1)
    state: dict[str, Any] = {
        "request_id": str(uuid.uuid4()),
        "workspace_id": "dev",
        "user_id": "dev",
        "topic": args.topic,
        "content_type": args.content_type,
        "target_accounts": [
            {"account_id": f"dev-{platform}", "platform": platform}
            for platform in platforms
        ],
        "publish_at": publish_at.isoformat(timespec="seconds"),
        "language": args.language,
    }
    if args.brand_voice:
        state["brand_voice"] = args.brand_voice
    return state  # type: ignore[return-value]


def _progress(record: StepRecord) -> None:
    print(
        f"  step {record.node_name} attempt {record.attempt}: {record.verdict}",
        file=sys.stderr,
    )


def text_report(result: dict[str, Any]) -> str:
    lines: list[str] = []
    for platform, draft in sorted((result.get("drafts") or {}).items()):
        lines.append(f"=== {platform} ===")
        if draft.get("title"):
            lines.append(f"Title: {draft['title']}")
        lines += [draft.get("caption", "(no caption)"), ""]
        lines.append("Hashtags: " + (" ".join(draft.get("hashtags") or []) or "(none)"))
        for item in draft.get("media") or []:
            meta = item.get("meta") or {}
            lines += [
                f"Image: {item.get('storage_key')} "
                f"({meta.get('width')}x{meta.get('height')}, {meta.get('mime_type')})",
                f"Alt text: {meta.get('alt_text')}",
            ]
        lines.append("")
    review = result.get("review") or {}
    if review:
        lines.append(
            f"=== review: {'ok' if review.get('ok') else 'NOT ok'} "
            f"(fix cycles: {result.get('review_cycles') or 0}) ==="
        )
        for issue in review.get("issues") or []:
            lines.append(
                f"- {issue['severity']} [{issue['responsible_node']}] "
                f"{issue.get('platform') or 'all'}: {issue['message']}"
            )
        lines += [f"- warning: {w}" for w in review.get("warnings") or []]
    return "\n".join(lines).rstrip()


def _interrupt_report(result: dict[str, Any]) -> dict[str, Any]:
    (interrupt,) = result["__interrupt__"]
    return dict(interrupt.value)


def run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    """Run the graph once; returns ``(exit_code, report)``."""
    if args.storage_dir:
        os.environ["STORAGE_PATH"] = args.storage_dir
        get_settings.cache_clear()
    state = build_state(args)
    if (get_settings().LLM_PROVIDER or "").strip().casefold() == "fake":
        platforms = sorted({a["platform"] for a in state["target_accounts"]})
        get_fake_script().set_default(
            post_flow_replies(args.topic, args.language, platforms)
        )
    graph = compile_graph(
        InMemorySaver(),
        post_subgraph=make_post_subgraph(),
        max_review_cycles=args.max_review_cycles,
    )
    config = thread_config(state["request_id"], step_recorder=_progress)
    result = graph.invoke(state, config)

    report: dict[str, Any] = {"request_id": state["request_id"]}
    if result.get("__interrupt__"):
        report["paused"] = _interrupt_report(result)
        return EXIT_PAUSED, report
    report.update(
        status=result.get("status"),
        errors=result.get("errors") or [],
        drafts=result.get("drafts") or {},
        review=result.get("review"),
        review_cycles=result.get("review_cycles") or 0,
    )
    return (EXIT_FAILED if result.get("status") == "failed" else EXIT_OK), report


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    code, report = run(args)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    elif code == EXIT_PAUSED:
        print(
            "Paused for a human:\n"
            + json.dumps(report["paused"], indent=2, default=str)
        )
    elif code == EXIT_FAILED:
        print("Request rejected or failed:")
        print("\n".join(f"- {e}" for e in report["errors"]) or "(no details)")
    else:
        print(text_report(report))
    return code


if __name__ == "__main__":  # pragma: no cover (exercised by a subprocess test)
    sys.exit(main())
