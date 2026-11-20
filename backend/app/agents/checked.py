"""Checked-node factory: agent -> check -> (pass | retry | escalate).

Every generating agent is wrapped by :func:`make_checked_node` so retry,
feedback passing, step logging and human escalation behave the same everywhere
(``ARCHITECTURE.md`` 6.3, ``AGENT_SPECS.md`` 1.5, decision D6).

One *attempt* is: the agent node runs, then the check node judges the result.

* **Rule checks first.** ``check_fn`` is cheap and mechanical. The optional
  ``judge_fn`` (an LLM judge) runs only if the rules passed, and its score must
  reach ``judge_threshold`` (``judge_pass_threshold``, default 0.7).
* **Retry.** A failed check stores its ``feedback`` in
  ``state["feedback"][key]`` and adds one to ``state["retries"][key]``. The
  agent reads that feedback on the next attempt. ``max_retries`` is the number
  of failed attempts a step may have before it escalates (default 3, video
  scene 2), so ``max_retries=3`` means three attempts in total.
* **Escalate.** When the limit is reached (or the failure cannot be fixed by
  trying again: a refusal or a non-retriable error) the escalate node calls
  LangGraph ``interrupt()`` with a ``needs_attention`` payload. The resume value
  is ``{"action": "retry" | "abort"}``. ``retry`` resets the step's counter
  (feedback is kept); ``abort`` ends the graph with ``status="failed"``. The
  worker (B3) turns the interrupt into the request status ``needs_attention``.
* **One ``agent_steps`` row per attempt**, written by the check node (or by the
  agent node when the agent itself failed): verdict ``passed``, ``retried``,
  ``escalated`` or ``error``; plus a ``failed`` row when a human aborts.
  Tokens, cost and model come from ``llm.capture_usage()`` and cover the agent
  *and* the judge. Rows are written through a recorder found in the run
  config (see :func:`resolve_step_recorder`); with none configured the graph
  still runs and nothing is stored.

Bookkeeping lives in three ``GraphState`` fields: ``retries``, ``feedback`` and
``attempt_info`` (in-flight facts about the last attempt of each step: attempt
number, usage, outcome). The state never holds secrets (``ARCHITECTURE.md`` 6.2).

Only domain errors (``AppError``: LLM provider/output errors and the like) are
treated as a failed attempt. Any other exception is a bug: it is recorded as an
``error`` step and re-raised so the run fails loudly instead of being retried.
"""

import logging
import re
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END
from langgraph.types import interrupt
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from app.agents.llm import LLMConfigError, capture_usage
from app.agents.state import GraphState
from app.core.errors import AppError
from app.core.logging import attempt_ctx, node_ctx, run_id_ctx
from app.services import runs

logger = logging.getLogger(__name__)

DEFAULT_MAX_RETRIES = 3
"""Failed attempts allowed per step (``system_settings.max_retries_default``)."""

DEFAULT_SCENE_MAX_RETRIES = 2
"""Same, for one video scene (``system_settings.max_retries_scene``)."""

DEFAULT_JUDGE_PASS_THRESHOLD = 0.7
"""Judge score needed to pass (``system_settings.judge_pass_threshold``)."""

MAX_ISSUE_MESSAGE_CHARS = 200
MAX_FEEDBACK_CHARS = 500
_MAX_KEEP_CHARS = 150
_MAX_STORED_ISSUES = 10
_GENERIC_FEEDBACK = (
    "The previous attempt did not pass the quality check. "
    "Follow the instructions and the output format exactly."
)

Outcome = Literal["pass", "retry", "escalate"]
Resolution = Literal["retry", "abort"]
EscalationReason = Literal["retries_exhausted", "refused", "non_retriable_error"]

RESOLUTIONS: tuple[str, ...] = ("retry", "abort")

# Fields only the factory may write: an agent that returned them would corrupt
# the retry bookkeeping.
RESERVED_STATE_KEYS = frozenset({"retries", "feedback", "attempt_info"})


# --------------------------------------------------------------------------
# Check result (AGENT_SPECS.md 1.5)
# --------------------------------------------------------------------------


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


class Issue(BaseModel):
    """One problem found by a check."""

    code: str  # stable machine code, e.g. "caption_too_long"
    message: str  # actionable, specific, <= 200 chars
    severity: Literal["error", "warning"]

    @field_validator("message")
    @classmethod
    def _limit_message(cls, value: str) -> str:
        return _clip(value, MAX_ISSUE_MESSAGE_CHARS)


def build_feedback(issues: Sequence[Issue], keep: str | None = None) -> str:
    """Join the error messages (at most 500 chars) and add ``Keep: ...``.

    Warnings never go into feedback: they do not block (AGENT_SPECS 1.5).
    """
    errors = " ".join(i.message for i in issues if i.severity == "error")
    keep_part = (
        f" Keep: {_clip(keep, _MAX_KEEP_CHARS)}" if keep and keep.strip() else ""
    )
    return (_clip(errors, MAX_FEEDBACK_CHARS - len(keep_part)) + keep_part).strip()


class CheckResult(BaseModel):
    """What every check (rule or judge) returns."""

    ok: bool  # False if any error-severity issue
    score: float | None = Field(default=None, ge=0, le=1)  # judges only
    issues: list[Issue] = Field(default_factory=list)
    feedback: str = ""  # max 500 chars; what the next attempt sees

    @field_validator("feedback")
    @classmethod
    def _limit_feedback(cls, value: str) -> str:
        return _clip(value, MAX_FEEDBACK_CHARS)

    @model_validator(mode="after")
    def _error_issue_means_not_ok(self) -> "CheckResult":
        if self.ok and any(i.severity == "error" for i in self.issues):
            self.ok = False
        return self

    @classmethod
    def from_issues(
        cls,
        issues: Sequence[Issue] = (),
        *,
        score: float | None = None,
        keep: str | None = None,
    ) -> "CheckResult":
        """Build a result from issues: ``ok`` iff there is no error issue."""
        issue_list = list(issues)
        ok = not any(i.severity == "error" for i in issue_list)
        return cls(
            ok=ok,
            score=score,
            issues=issue_list,
            feedback="" if ok else build_feedback(issue_list, keep),
        )


# --------------------------------------------------------------------------
# What an agent returns, and how steps are recorded
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentOutput:
    """Optional rich return value of an ``agent_fn``.

    A plain ``dict`` (the state update) is accepted too. ``prompt_version`` goes
    to ``agent_steps.output_summary``; ``summary`` is a small, secret-free
    description of the output (it is trimmed and redacted before storage).
    ``refused_reason`` is the agent's ``refused=true`` answer (AGENT_SPECS 1.7):
    the step escalates at once instead of retrying.
    """

    update: dict[str, Any]
    prompt_version: str | None = None
    summary: Any = None
    refused_reason: str | None = None


AgentFn = Callable[[GraphState], "dict[str, Any] | AgentOutput"]
CheckFn = Callable[[GraphState], CheckResult]


@dataclass(frozen=True)
class StepRecord:
    """Plain values of one ``agent_steps`` row (see ``services/runs.record_step``)."""

    node_name: str
    attempt: int
    verdict: str
    feedback: str | None
    input_summary: Any
    output_summary: Any
    tokens_in: int
    tokens_out: int
    cost_usd: float | None
    latency_ms: int
    model: str | None


StepRecorder = Callable[[StepRecord], None]


def db_step_recorder(
    run_id: uuid.UUID,
    session_factory: Callable[[], Session] | None = None,
) -> StepRecorder:
    """Recorder that writes each step to ``agent_steps`` in its own transaction."""

    def record(step: StepRecord) -> None:
        factory = session_factory
        if factory is None:
            from app.core.db import SessionLocal  # lazy: no engine for offline runs

            factory = SessionLocal
        with factory() as db:
            run = runs.get_run(db, run_id)
            runs.record_step(
                db,
                run,
                step.node_name,
                step.attempt,
                step.verdict,
                feedback=step.feedback,
                input_summary=step.input_summary,
                output_summary=step.output_summary,
                tokens_in=step.tokens_in,
                tokens_out=step.tokens_out,
                cost_usd=step.cost_usd,
                latency_ms=step.latency_ms,
                model=step.model,
            )
            db.commit()

    return record


def _configurable(config: RunnableConfig | None) -> Mapping[str, Any]:
    return (config or {}).get("configurable") or {}


def resolve_step_recorder(config: RunnableConfig | None) -> StepRecorder | None:
    """Find the recorder for this run.

    ``config["configurable"]["step_recorder"]`` (a callable, used by tests and
    the dev runner) wins; otherwise ``config["configurable"]["run_id"]`` selects
    the database recorder; otherwise steps are not stored.
    """
    configurable = _configurable(config)
    custom = configurable.get("step_recorder")
    if custom is not None:
        return custom  # type: ignore[no-any-return]
    run_id = configurable.get("run_id")
    if run_id:
        return db_step_recorder(uuid.UUID(str(run_id)))
    return None


def _record(recorder: StepRecorder | None, step: StepRecord) -> None:
    """Store a step. Monitoring must never break generation, so failures are logged."""
    if recorder is None:
        return
    try:
        recorder(step)
    except Exception:
        logger.exception("Could not record agent step")


@contextmanager
def _log_context(
    node: str, attempt: int, config: RunnableConfig | None
) -> Iterator[None]:
    """Put node, attempt and run id on every log line written inside the block."""
    tokens = [(node_ctx, node_ctx.set(node)), (attempt_ctx, attempt_ctx.set(attempt))]
    run_id = _configurable(config).get("run_id")
    if run_id:
        tokens.append((run_id_ctx, run_id_ctx.set(str(run_id))))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


def node_name(key: str) -> str:
    """Graph node name for a step key (LangGraph reserves ``:`` and ``|``)."""
    return re.sub(r"[^A-Za-z0-9_]", "_", key)


def _error_info(exc: AppError) -> dict[str, Any]:
    retriable = bool(getattr(exc, "retriable", True)) and not isinstance(
        exc, LLMConfigError
    )
    return {"code": exc.code, "retriable": retriable}


def _issue_dicts(issues: Sequence[Issue]) -> list[dict[str, str]]:
    return [i.model_dump() for i in issues[:_MAX_STORED_ISSUES]]


def decide(
    *,
    passed: bool,
    retriable: bool,
    refused: bool,
    retries_after: int,
    max_retries: int,
) -> Outcome:
    """The routing rule: pass; else retry while under the limit; else escalate."""
    if passed:
        return "pass"
    if refused or not retriable:
        return "escalate"
    return "retry" if retries_after < max_retries else "escalate"


def _merge_models(*groups: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(m for group in groups for m in group))


def _sum_cost(*costs: float | None) -> float | None:
    known = [c for c in costs if c is not None]
    return sum(known) if known else None


def _parse_resolution(value: Any) -> Resolution | None:
    """The action in a resume value, or ``None`` if it is not a valid one."""
    action = value.get("action") if isinstance(value, Mapping) else None
    return action if action in RESOLUTIONS else None  # type: ignore[return-value]


# --------------------------------------------------------------------------
# The factory
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckedNode:
    """The pieces of one checked step. Wire them with :func:`add_checked_node`."""

    key: str
    max_retries: int
    agent: Callable[..., dict[str, Any]]
    check: Callable[..., dict[str, Any]]
    escalate: Callable[..., dict[str, Any]]
    route: Callable[[GraphState], Outcome]
    route_resolution: Callable[[GraphState], Resolution]

    @property
    def agent_name(self) -> str:
        return node_name(self.key)

    @property
    def check_name(self) -> str:
        return f"check_{node_name(self.key)}"

    @property
    def escalate_name(self) -> str:
        return f"escalate_{node_name(self.key)}"


def make_checked_node(
    key: str,
    agent_fn: AgentFn,
    check_fn: CheckFn,
    max_retries: int = DEFAULT_MAX_RETRIES,
    *,
    judge_fn: CheckFn | None = None,
    judge_threshold: float = DEFAULT_JUDGE_PASS_THRESHOLD,
) -> CheckedNode:
    """Wrap ``agent_fn`` with its checks, retry loop and escalation.

    * ``key``: step key used in ``state["retries"]``/``["feedback"]`` and as
      ``agent_steps.node_name`` (``"writer"``, ``"scene:2"``).
    * ``agent_fn(state)``: reads ``state["feedback"].get(key)`` and returns the
      state update (or an :class:`AgentOutput`). May raise ``AppError``.
    * ``check_fn(state)``: rule checks on the new state; returns a
      :class:`CheckResult`.
    * ``judge_fn(state)``: optional LLM judge, called only if the rules pass.
    * ``max_retries``: failed attempts before escalation (default 3).
    """
    if not key or not key.strip():
        raise ValueError("key must not be empty")
    if (
        isinstance(max_retries, bool)
        or not isinstance(max_retries, int)
        or max_retries < 1
    ):
        raise ValueError("max_retries must be an integer >= 1")
    if not 0 <= judge_threshold <= 1:
        raise ValueError("judge_threshold must be between 0 and 1")

    def run_checks(state: GraphState) -> CheckResult:
        result = check_fn(state)
        if not isinstance(result, CheckResult):
            raise TypeError("check_fn must return a CheckResult")
        if not result.ok or judge_fn is None:
            return result

        judged = judge_fn(state)
        if not isinstance(judged, CheckResult):
            raise TypeError("judge_fn must return a CheckResult")
        issues = [*result.issues, *judged.issues]
        score = judged.score
        feedback = judged.feedback
        ok = judged.ok
        if ok and score is not None and score < judge_threshold:
            ok = False
            issues.append(
                Issue(
                    code="judge_score_low",
                    message=(
                        f"Overall quality score {score:.2f} is below the required "
                        f"{judge_threshold:.2f}. Fix the problems and improve it."
                    ),
                    severity="error",
                )
            )
            feedback = build_feedback(issues, None) or feedback
        return CheckResult(ok=ok, score=score, issues=issues, feedback=feedback)

    def agent_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
        infos = dict(state.get("attempt_info") or {})
        previous = infos.get(key) or {}
        attempt = int(previous.get("attempt", 0)) + 1
        prior_feedback = (state.get("feedback") or {}).get(key)
        recorder = resolve_step_recorder(config)

        info: dict[str, Any] = {
            "attempt": attempt,
            "outcome": None,
            "input_feedback": prior_feedback,
            "prompt_version": None,
            "summary": None,
            "error": None,
            "refused": None,
            "issues": [],
            "score": None,
            "escalation_reason": None,
            "resolution": None,
        }
        update: dict[str, Any] = {}
        started = time.perf_counter()
        with _log_context(key, attempt, config), capture_usage() as usage:
            try:
                produced = agent_fn(state)
                output = (
                    produced
                    if isinstance(produced, AgentOutput)
                    else AgentOutput(update=produced)
                )
                clash = RESERVED_STATE_KEYS & output.update.keys()
                if clash:
                    raise ValueError(
                        f"agent {key!r} must not write {sorted(clash)}: "
                        "retry bookkeeping belongs to the checked node"
                    )
                update = dict(output.update)
                info["prompt_version"] = output.prompt_version
                info["summary"] = runs.trim_summary(output.summary)
                info["refused"] = output.refused_reason
            except GraphBubbleUp:
                raise
            except AppError as exc:
                info["error"] = _error_info(exc)
                logger.warning("Agent attempt failed with %s", exc.code)
            except Exception as exc:
                _record(
                    recorder,
                    StepRecord(
                        node_name=key,
                        attempt=attempt,
                        verdict="error",
                        feedback=f"Unexpected {type(exc).__name__}",
                        input_summary={"prior_feedback": prior_feedback},
                        output_summary={"error_type": type(exc).__name__},
                        tokens_in=usage.tokens_in,
                        tokens_out=usage.tokens_out,
                        cost_usd=usage.cost_usd,
                        latency_ms=usage.latency_ms,
                        model=", ".join(usage.models) or None,
                    ),
                )
                raise

        info.update(
            tokens_in=usage.tokens_in,
            tokens_out=usage.tokens_out,
            cost_usd=usage.cost_usd,
            latency_ms=usage.latency_ms,
            models=list(usage.models),
            duration_ms=round((time.perf_counter() - started) * 1000),
        )
        infos[key] = info
        return {**update, "attempt_info": infos}

    def check_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
        infos = dict(state.get("attempt_info") or {})
        info = dict(infos.get(key) or {})
        if not info or "attempt" not in info:
            raise RuntimeError(f"check node for {key!r} ran before its agent node")
        attempt = int(info["attempt"])
        recorder = resolve_step_recorder(config)

        error: dict[str, Any] | None = info.get("error")
        refused: str | None = info.get("refused")
        result: CheckResult | None = None
        started = time.perf_counter()
        with _log_context(key, attempt, config), capture_usage() as usage:
            if error is None and refused is None:
                try:
                    result = run_checks(state)
                except GraphBubbleUp:
                    raise
                except AppError as exc:
                    error = _error_info(exc)
                    logger.warning("Check failed with %s", exc.code)
                except Exception as exc:
                    _record(
                        recorder,
                        StepRecord(
                            node_name=key,
                            attempt=attempt,
                            verdict="error",
                            feedback=f"Unexpected {type(exc).__name__} in check",
                            input_summary={
                                "prior_feedback": info.get("input_feedback")
                            },
                            output_summary={"error_type": type(exc).__name__},
                            tokens_in=int(info.get("tokens_in", 0)) + usage.tokens_in,
                            tokens_out=int(info.get("tokens_out", 0))
                            + usage.tokens_out,
                            cost_usd=_sum_cost(info.get("cost_usd"), usage.cost_usd),
                            latency_ms=int(info.get("latency_ms", 0))
                            + usage.latency_ms,
                            model=", ".join(
                                _merge_models(info.get("models", []), usage.models)
                            )
                            or None,
                        ),
                    )
                    raise

        passed = result is not None and result.ok
        retries = dict(state.get("retries") or {})
        feedback_map = dict(state.get("feedback") or {})
        retries_after = int(retries.get(key, 0)) + (0 if passed else 1)

        outcome = decide(
            passed=passed,
            retriable=error is None or bool(error.get("retriable", True)),
            refused=refused is not None,
            retries_after=retries_after,
            max_retries=max_retries,
        )

        step_feedback: str | None = None
        reason: EscalationReason | None = None
        if passed:
            verdict = "passed"
            feedback_map[key] = None
        else:
            if refused is not None:
                step_feedback = f"Refused: {refused}"
            elif error is not None:
                # Keep the feedback the agent already has: an outage says
                # nothing about how to improve the output.
                step_feedback = f"Error: {error['code']}"
            else:
                assert result is not None
                step_feedback = (
                    result.feedback.strip()
                    or build_feedback(result.issues)
                    or _GENERIC_FEEDBACK
                )
                feedback_map[key] = step_feedback  # the agent sees this next
            if outcome == "escalate":
                if refused is not None:
                    reason = "refused"
                elif error is not None and not error.get("retriable", True):
                    reason = "non_retriable_error"
                else:
                    reason = "retries_exhausted"
            verdict = (
                "escalated"
                if outcome == "escalate"
                else ("error" if error is not None else "retried")
            )
            retries[key] = retries_after

        issues = result.issues if result is not None else []
        info.update(
            outcome=outcome,
            escalation_reason=reason,
            issues=_issue_dicts(issues),
            score=result.score if result is not None else None,
            error=error,
            feedback=step_feedback,
        )
        info_models = _merge_models(info.get("models", []), usage.models)
        duration_ms = int(info.get("duration_ms", 0)) + round(
            (time.perf_counter() - started) * 1000
        )
        info["duration_ms"] = duration_ms
        infos[key] = info

        _record(
            recorder,
            StepRecord(
                node_name=key,
                attempt=attempt,
                verdict=verdict,
                feedback=step_feedback,
                input_summary={
                    "prior_feedback": info.get("input_feedback"),
                    "retries_before": int((state.get("retries") or {}).get(key, 0)),
                },
                output_summary={
                    "prompt_version": info.get("prompt_version"),
                    "summary": info.get("summary"),
                    "score": info["score"],
                    "issues": info["issues"],
                    "error_code": error["code"] if error else None,
                    "refused": refused,
                    "outcome": outcome,
                    "escalation_reason": reason,
                    "duration_ms": duration_ms,
                },
                tokens_in=int(info.get("tokens_in", 0)) + usage.tokens_in,
                tokens_out=int(info.get("tokens_out", 0)) + usage.tokens_out,
                cost_usd=_sum_cost(info.get("cost_usd"), usage.cost_usd),
                latency_ms=int(info.get("latency_ms", 0)) + usage.latency_ms,
                model=", ".join(info_models) or None,
            ),
        )
        logger.info(
            "Checked step %s attempt %d: %s",
            key,
            attempt,
            verdict,
            extra={"duration_ms": duration_ms},
        )
        return {"retries": retries, "feedback": feedback_map, "attempt_info": infos}

    def escalate_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
        infos = dict(state.get("attempt_info") or {})
        info = dict(infos.get(key) or {})
        attempt = int(info.get("attempt", 1))
        feedback_now = (state.get("feedback") or {}).get(key)

        payload = {
            "type": "needs_attention",
            "step": key,
            "reason": info.get("escalation_reason") or "retries_exhausted",
            "attempts": attempt,
            "max_retries": max_retries,
            "feedback": feedback_now,
            "error_code": (info.get("error") or {}).get("code"),
            "refused": info.get("refused"),
            "issues": info.get("issues", []),
        }
        # Everything above interrupt() runs again on resume, so it only builds
        # the payload; anything written to the database comes after the loop.
        # An invalid resume value must not be raised as an error: LangGraph
        # keeps it in the checkpoint and would replay it on every later
        # resume, leaving the request stuck. Ask again instead; each
        # interrupt() call consumes the next resume value in order.
        action = _parse_resolution(interrupt(payload))
        while action is None:
            action = _parse_resolution(
                interrupt(
                    {
                        **payload,
                        "error": "invalid_resolution",
                        "allowed": list(RESOLUTIONS),
                    }
                )
            )
        info["resolution"] = action
        infos[key] = info

        if action == "retry":
            retries = dict(state.get("retries") or {})
            retries[key] = 0
            info["outcome"] = None
            return {"retries": retries, "attempt_info": infos, "status": "generating"}

        _record(
            resolve_step_recorder(config),
            StepRecord(
                node_name=key,
                attempt=attempt,
                verdict="failed",
                feedback=feedback_now,
                input_summary={"resolution": "abort"},
                output_summary={"reason": info.get("escalation_reason")},
                tokens_in=0,
                tokens_out=0,
                cost_usd=None,
                latency_ms=0,
                model=None,
            ),
        )
        errors = list(state.get("errors") or [])
        errors.append(f"Step '{key}' was aborted by a human after attempt {attempt}.")
        return {"attempt_info": infos, "status": "failed", "errors": errors}

    def route(state: GraphState) -> Outcome:
        outcome = (state.get("attempt_info") or {}).get(key, {}).get("outcome")
        if outcome not in ("pass", "retry", "escalate"):
            raise RuntimeError(f"route for {key!r} called before its check ran")
        return outcome  # type: ignore[no-any-return]

    def route_resolution(state: GraphState) -> Resolution:
        resolution = (state.get("attempt_info") or {}).get(key, {}).get("resolution")
        if resolution not in RESOLUTIONS:
            raise RuntimeError(f"escalation of {key!r} has no resolution")
        return resolution  # type: ignore[no-any-return]

    return CheckedNode(
        key=key,
        max_retries=max_retries,
        agent=agent_node,
        check=check_node,
        escalate=escalate_node,
        route=route,
        route_resolution=route_resolution,
    )


def add_checked_node(
    graph: Any,
    checked: CheckedNode,
    *,
    on_pass: str,
    on_abort: str = END,
) -> str:
    """Add the agent, check and escalate nodes of ``checked`` to ``graph``.

    Wires ``agent -> check``; the check goes to ``on_pass``, back to the agent
    (retry) or to the escalate node; after a human resolves it the graph either
    retries the agent or goes to ``on_abort`` (the end by default). Returns the
    name of the entry node: point the previous node's edge at it.
    """
    graph.add_node(checked.agent_name, checked.agent)
    graph.add_node(checked.check_name, checked.check)
    graph.add_node(checked.escalate_name, checked.escalate)
    graph.add_edge(checked.agent_name, checked.check_name)
    graph.add_conditional_edges(
        checked.check_name,
        checked.route,
        {
            "pass": on_pass,
            "retry": checked.agent_name,
            "escalate": checked.escalate_name,
        },
    )
    graph.add_conditional_edges(
        checked.escalate_name,
        checked.route_resolution,
        {"retry": checked.agent_name, "abort": on_abort},
    )
    return checked.agent_name
