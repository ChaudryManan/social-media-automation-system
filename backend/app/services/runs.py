"""Workflow run bookkeeping (DATABASE.md 4.10 and 4.11, ARCHITECTURE.md 6.3).

* ``start_run`` creates a ``workflow_runs`` row each time generation is
  (re)started.
* ``record_step`` writes one ``agent_steps`` row per node attempt. It takes
  plain numbers (tokens, cost, latency, model) so that this layer does not
  depend on ``app.agents``; the checked node copies them from
  ``llm.capture_usage()``.
* ``set_run_status`` is the only way a run changes status; terminal statuses
  stamp ``finished_at`` and store a summary computed from the steps.

Like ``services.audit.log``, these functions flush but never commit: the
caller owns the transaction.

Privacy: step summaries are redacted (``audit.redact_sensitive``) and bounded
in size before they are stored, so secrets and full prompts stay out of the
database (DATABASE.md section 7, AGENTS.md section 3).
"""

import json
import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError
from app.models.runs import RUN_STATUSES, STEP_VERDICTS, AgentStep, WorkflowRun
from app.services.audit import redact_sensitive

logger = logging.getLogger(__name__)

# Terminal statuses are final: a new run is created when generation is
# restarted (DATABASE.md 4.10). ``running`` and ``paused`` can go anywhere.
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})

MAX_SUMMARY_STRING = 500
MAX_SUMMARY_ITEMS = 20
MAX_SUMMARY_DEPTH = 5
MAX_SUMMARY_JSON_CHARS = 8000
_TRUNCATED_PREVIEW_CHARS = 2000
MAX_FEEDBACK_CHARS = 4000
MAX_ERROR_CHARS = 2000

_COST_QUANTUM = Decimal("0.000001")


# --------------------------------------------------------------------------
# Summary trimming
# --------------------------------------------------------------------------


def _trim(value: Any, depth: int) -> Any:
    if isinstance(value, str):
        if len(value) > MAX_SUMMARY_STRING:
            return value[:MAX_SUMMARY_STRING] + "…"
        return value
    if value is None or isinstance(value, bool | int | float):
        return value
    if depth >= MAX_SUMMARY_DEPTH:
        return "…"
    if isinstance(value, dict):
        items = list(value.items())
        trimmed = {str(k): _trim(v, depth + 1) for k, v in items[:MAX_SUMMARY_ITEMS]}
        if len(items) > MAX_SUMMARY_ITEMS:
            trimmed["_more"] = len(items) - MAX_SUMMARY_ITEMS
        return trimmed
    if isinstance(value, list):
        trimmed_list = [_trim(v, depth + 1) for v in value[:MAX_SUMMARY_ITEMS]]
        if len(value) > MAX_SUMMARY_ITEMS:
            trimmed_list.append(f"…(+{len(value) - MAX_SUMMARY_ITEMS} more)")
        return trimmed_list
    return _trim(str(value), depth)


def trim_summary(value: Any) -> Any:
    """Return a redacted, size-bounded, JSON-safe copy of ``value``.

    Secrets are redacted by key name, long strings are cut, big lists and
    dicts are truncated, and anything still larger than
    ``MAX_SUMMARY_JSON_CHARS`` is replaced by a short preview.
    """
    if value is None:
        return None
    trimmed = _trim(redact_sensitive(value), depth=0)
    encoded = json.dumps(trimmed, default=str)
    if len(encoded) > MAX_SUMMARY_JSON_CHARS:
        return {"_truncated": True, "preview": encoded[:_TRUNCATED_PREVIEW_CHARS]}
    return trimmed


# --------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------


def start_run(
    db: Session,
    request_id: uuid.UUID,
    *,
    thread_id: str | None = None,
) -> WorkflowRun:
    """Create a ``running`` run for ``request_id`` (the caller commits).

    ``thread_id`` is the LangGraph thread id; it defaults to the request id
    (ARCHITECTURE.md 6.5).
    """
    run = WorkflowRun(
        request_id=request_id,
        thread_id=thread_id or str(request_id),
        status="running",
    )
    db.add(run)
    db.flush()
    logger.info("Workflow run started", extra={"run_id": str(run.id)})
    return run


def get_run(db: Session, run_id: uuid.UUID) -> WorkflowRun:
    """Return the run or raise ``NotFoundError`` (``run_not_found``)."""
    run = db.get(WorkflowRun, run_id)
    if run is None:
        raise NotFoundError("Workflow run not found", code="run_not_found")
    return run


def latest_run(db: Session, request_id: uuid.UUID) -> WorkflowRun | None:
    """Return the most recently created run of a request, if any."""
    return db.scalars(
        select(WorkflowRun)
        .where(WorkflowRun.request_id == request_id)
        .order_by(WorkflowRun.created_at.desc())
        .limit(1)
    ).first()


def list_steps(db: Session, run_id: uuid.UUID) -> list[AgentStep]:
    """Return the steps of a run in the order they were recorded."""
    return list(
        db.scalars(
            select(AgentStep)
            .where(AgentStep.run_id == run_id)
            .order_by(AgentStep.created_at)
        )
    )


def summarize_run(db: Session, run: WorkflowRun) -> dict[str, Any]:
    """Aggregate the steps of ``run`` (DATABASE.md 4.10 ``summary``).

    ``cost_usd`` is ``None`` when no step reported a cost.
    """
    rows = db.execute(
        select(
            AgentStep.verdict,
            func.count(),
            func.coalesce(func.sum(AgentStep.tokens_in), 0),
            func.coalesce(func.sum(AgentStep.tokens_out), 0),
            func.coalesce(func.sum(AgentStep.latency_ms), 0),
            func.sum(AgentStep.cost_usd),
        )
        .where(AgentStep.run_id == run.id)
        .group_by(AgentStep.verdict)
    ).all()

    verdicts: dict[str, int] = {}
    tokens_in = tokens_out = latency_ms = 0
    cost: Decimal | None = None
    for verdict, count, t_in, t_out, latency, step_cost in rows:
        verdicts[verdict] = int(count)
        tokens_in += int(t_in)
        tokens_out += int(t_out)
        latency_ms += int(latency)
        if step_cost is not None:
            cost = (cost or Decimal(0)) + Decimal(step_cost)

    return {
        "steps": sum(verdicts.values()),
        "verdicts": verdicts,
        "retries": verdicts.get("retried", 0),
        "escalations": verdicts.get("escalated", 0),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": float(round(cost, 6)) if cost is not None else None,
        "latency_ms": latency_ms,
    }


def set_run_status(
    db: Session,
    run: WorkflowRun,
    status: str,
    *,
    error: str | None = None,
) -> WorkflowRun:
    """Move ``run`` to ``status`` (the caller commits).

    ``running`` and ``paused`` may alternate; ``completed``, ``failed`` and
    ``cancelled`` are final and set ``finished_at`` and ``summary``. Asking for
    the status the run already has is a no-op; changing a finished run raises
    ``ConflictError`` (``run_finished``).
    """
    if status not in RUN_STATUSES:
        raise ValueError(f"Invalid run status: {status!r}")
    if status == run.status:
        return run
    if run.status in TERMINAL_STATUSES:
        raise ConflictError(
            f"Run is already {run.status}",
            code="run_finished",
            details={"status": run.status},
        )

    run.status = status
    if error is not None:
        run.error = error[:MAX_ERROR_CHARS]
    if status in TERMINAL_STATUSES:
        run.finished_at = datetime.now(UTC)
        run.summary = summarize_run(db, run)
    db.flush()
    logger.info("Workflow run status changed", extra={"run_id": str(run.id)})
    return run


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------


def _non_negative_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _cost(value: float | Decimal | None) -> Decimal | None:
    if value is None:
        return None
    cost = Decimal(str(value))
    if not cost.is_finite() or cost < 0:
        raise ValueError("cost_usd must be a non-negative number")
    return cost.quantize(_COST_QUANTUM)


def record_step(
    db: Session,
    run: WorkflowRun,
    node_name: str,
    attempt: int,
    verdict: str,
    *,
    feedback: str | None = None,
    input_summary: Any = None,
    output_summary: Any = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: float | Decimal | None = None,
    latency_ms: int = 0,
    model: str | None = None,
) -> AgentStep:
    """Record one attempt of one node in ``run`` (the caller commits).

    ``attempt`` is 1-based. ``request_id`` is copied from the run so the two
    can never disagree. Summaries are redacted and trimmed. Steps can only be
    added to a run that is still ``running`` or ``paused``.
    """
    node = node_name.strip() if isinstance(node_name, str) else ""
    if not node:
        raise ValueError("node_name must not be empty")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ValueError("attempt must be an integer >= 1")
    if verdict not in STEP_VERDICTS:
        raise ValueError(f"Invalid step verdict: {verdict!r}")
    if run.status in TERMINAL_STATUSES:
        raise ConflictError(
            f"Run is already {run.status}",
            code="run_finished",
            details={"status": run.status},
        )

    step = AgentStep(
        run=run,
        request_id=run.request_id,
        node_name=node,
        attempt=attempt,
        verdict=verdict,
        feedback=feedback[:MAX_FEEDBACK_CHARS] if feedback is not None else None,
        input_summary=trim_summary(input_summary),
        output_summary=trim_summary(output_summary),
        tokens_in=_non_negative_int("tokens_in", tokens_in),
        tokens_out=_non_negative_int("tokens_out", tokens_out),
        cost_usd=_cost(cost_usd),
        latency_ms=_non_negative_int("latency_ms", latency_ms),
        model=model,
    )
    db.add(step)
    db.flush()
    return step
