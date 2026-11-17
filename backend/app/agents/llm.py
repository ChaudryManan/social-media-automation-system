"""LLM access layer: the only place agent code talks to a language model.

* ``get_chat_model(kind)`` returns a provider-backed :class:`ChatModel`; the
  provider and model names come from settings (``LLM_PROVIDER``,
  ``LLM_MODEL_TEXT``, ``LLM_MODEL_FAST``), never from code.
* ``generate_structured(schema, messages)`` returns a validated Pydantic
  object, with exactly one automatic repair retry when the reply is not valid
  JSON or does not match the schema (``AGENT_SPECS.md`` 1.1).
* ``capture_usage()`` collects tokens, cost and latency of every call made
  inside the ``with`` block, so a checked node can store them in
  ``agent_steps`` (``DATABASE.md`` 4.11).
* The ``fake`` provider is scripted (:class:`FakeScript`): queue responses,
  make the next N calls fail, inspect the prompts it received. Tests and the
  offline dev runner use it; no network is ever touched.

The wrapper is a small interface of our own (not a LangChain class) so that the
fake provider and the tests need no provider SDK. A real provider (Gemini or
another) is added with :func:`register_provider` and must implement
:meth:`ChatModel._generate`; nothing else in the codebase changes.

Privacy: prompts and model replies are never logged and never put in error
messages. Only provider, model, token counts and latency are logged.
"""

import json
import logging
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from app.core.config import Settings, get_settings
from app.core.errors import AppError

logger = logging.getLogger(__name__)

LLMKind = Literal["text", "fast"]
Role = Literal["system", "user", "assistant"]

_KINDS: tuple[str, ...] = ("text", "fast")
_ROLES: tuple[str, ...] = ("system", "user", "assistant")

MAX_REPAIR_RETRIES = 1
"""Extra attempts ``generate_structured`` makes after an unusable reply."""

_REPAIR_ECHO_LIMIT = 8000
_MAX_PROBLEMS = 10

T = TypeVar("T", bound=BaseModel)


# --------------------------------------------------------------------------
# Errors (stable codes, see AGENTS.md section 4)
# --------------------------------------------------------------------------


class LLMError(AppError):
    """Base class for LLM layer failures."""

    def __init__(
        self,
        message: str = "LLM call failed",
        code: str = "llm_error",
        http_status: int = 502,
        details: dict[str, Any] | list[Any] | None = None,
    ) -> None:
        super().__init__(
            code=code, message=message, http_status=http_status, details=details
        )


class LLMConfigError(LLMError):
    """The LLM provider or model is not configured (or not available)."""

    def __init__(
        self,
        message: str = "LLM is not configured",
        details: dict[str, Any] | list[Any] | None = None,
    ) -> None:
        super().__init__(
            message=message,
            code="llm_not_configured",
            http_status=500,
            details=details,
        )


class LLMProviderError(LLMError):
    """The provider call itself failed (network, quota, server error...).

    ``retriable`` tells the caller whether trying again can help. The wrapper
    does not retry provider failures; the checked-node pattern (B2-04) decides.
    """

    def __init__(
        self,
        message: str = "LLM provider call failed",
        retriable: bool = True,
        details: dict[str, Any] | list[Any] | None = None,
    ) -> None:
        super().__init__(
            message=message,
            code="llm_provider_error",
            http_status=502,
            details=details,
        )
        self.retriable = retriable


class LLMOutputError(LLMError):
    """The model replied, but never with output that matches the schema."""

    def __init__(
        self,
        message: str = "LLM output did not match the expected schema",
        details: dict[str, Any] | list[Any] | None = None,
    ) -> None:
        super().__init__(
            message=message,
            code="llm_invalid_output",
            http_status=502,
            details=details,
        )


class FakeScriptExhaustedError(LLMError):
    """The fake provider was called with nothing scripted for that call."""

    def __init__(self, message: str = "Fake LLM script is exhausted") -> None:
        super().__init__(
            message=message, code="fake_llm_script_exhausted", http_status=500
        )


# --------------------------------------------------------------------------
# Value types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Message:
    """One chat message."""

    role: Role
    content: str

    def __post_init__(self) -> None:
        if self.role not in _ROLES:
            raise ValueError(f"Invalid message role: {self.role!r}")
        if not isinstance(self.content, str):
            raise TypeError("Message content must be a string")


def system_message(content: str) -> Message:
    return Message("system", content)


def user_message(content: str) -> Message:
    return Message("user", content)


def assistant_message(content: str) -> Message:
    return Message("assistant", content)


@dataclass(frozen=True)
class LLMCompletion:
    """What a provider returns from ``_generate``."""

    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float | None = None


@dataclass(frozen=True)
class LLMUsage:
    """Accounting for one successful model call."""

    provider: str
    model: str
    kind: str
    tokens_in: int
    tokens_out: int
    cost_usd: float | None
    latency_ms: int


@dataclass(frozen=True)
class LLMResponse:
    """A model reply plus its usage."""

    text: str
    usage: LLMUsage


# --------------------------------------------------------------------------
# Usage capture
# --------------------------------------------------------------------------


class UsageRecorder:
    """Collects the :class:`LLMUsage` of every call made while it is active."""

    def __init__(self) -> None:
        self.records: list[LLMUsage] = []

    def record(self, usage: LLMUsage) -> None:
        self.records.append(usage)

    @property
    def calls(self) -> int:
        return len(self.records)

    @property
    def tokens_in(self) -> int:
        return sum(r.tokens_in for r in self.records)

    @property
    def tokens_out(self) -> int:
        return sum(r.tokens_out for r in self.records)

    @property
    def latency_ms(self) -> int:
        return sum(r.latency_ms for r in self.records)

    @property
    def cost_usd(self) -> float | None:
        """Sum of the known costs, or ``None`` if no call reported a cost."""
        known = [r.cost_usd for r in self.records if r.cost_usd is not None]
        return sum(known) if known else None

    @property
    def models(self) -> tuple[str, ...]:
        """Distinct model names used, in first-use order."""
        return tuple(dict.fromkeys(r.model for r in self.records))


_recorders: ContextVar[tuple[UsageRecorder, ...]] = ContextVar(
    "llm_usage_recorders", default=()
)


@contextmanager
def capture_usage() -> Iterator[UsageRecorder]:
    """Record usage of all model calls made inside the ``with`` block.

    Blocks can be nested: every active recorder sees every call. Recorders are
    bound to the current context, so concurrent tasks do not mix their usage.
    """
    recorder = UsageRecorder()
    token = _recorders.set((*_recorders.get(), recorder))
    try:
        yield recorder
    finally:
        _recorders.reset(token)


def _emit_usage(usage: LLMUsage) -> None:
    for recorder in _recorders.get():
        recorder.record(usage)


# --------------------------------------------------------------------------
# Provider interface
# --------------------------------------------------------------------------


class ChatModel(ABC):
    """A chat model bound to one provider and one model name.

    Subclasses implement :meth:`_generate` only. ``invoke`` adds timing, usage
    capture, logging and uniform error mapping.
    """

    provider: str = "unknown"

    def __init__(self, *, model: str, kind: LLMKind) -> None:
        self.model = model
        self.kind: LLMKind = kind

    def invoke(
        self,
        messages: Sequence[Message],
        *,
        temperature: float | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """Call the model once. ``json_schema`` is a hint for native JSON modes."""
        prompt = list(messages)
        if not prompt:
            raise ValueError("messages must not be empty")

        started = time.perf_counter()
        try:
            completion = self._generate(
                prompt, temperature=temperature, json_schema=json_schema
            )
        except AppError:
            logger.warning(
                "LLM call failed provider=%s model=%s", self.provider, self.model
            )
            raise
        except Exception as exc:
            logger.warning(
                "LLM call failed provider=%s model=%s error=%s",
                self.provider,
                self.model,
                type(exc).__name__,
            )
            raise LLMProviderError(
                "LLM provider call failed",
                details={"error_type": type(exc).__name__},
            ) from exc

        latency_ms = max(0, round((time.perf_counter() - started) * 1000))
        usage = LLMUsage(
            provider=self.provider,
            model=self.model,
            kind=self.kind,
            tokens_in=completion.tokens_in,
            tokens_out=completion.tokens_out,
            cost_usd=completion.cost_usd,
            latency_ms=latency_ms,
        )
        _emit_usage(usage)
        logger.info(
            "LLM call ok provider=%s model=%s kind=%s tokens_in=%d "
            "tokens_out=%d latency_ms=%d",
            usage.provider,
            usage.model,
            usage.kind,
            usage.tokens_in,
            usage.tokens_out,
            usage.latency_ms,
        )
        return LLMResponse(text=completion.text, usage=usage)

    @abstractmethod
    def _generate(
        self,
        messages: list[Message],
        *,
        temperature: float | None,
        json_schema: dict[str, Any] | None,
    ) -> LLMCompletion:
        """Provider-specific call. Raise ``LLMProviderError`` on failure."""


# --------------------------------------------------------------------------
# Fake provider (scripted)
# --------------------------------------------------------------------------

FakeResponse = str | dict[str, Any] | list[Any] | BaseModel | BaseException


@dataclass(frozen=True)
class FakeCall:
    """One call received by the fake provider (kept in ``FakeScript.calls``)."""

    kind: str
    model: str
    temperature: float | None
    messages: tuple[Message, ...]
    json_schema: dict[str, Any] | None

    @property
    def prompt_text(self) -> str:
        """All message contents joined, handy for ``in`` assertions."""
        return "\n".join(m.content for m in self.messages)


def _to_text(response: FakeResponse) -> str:
    if isinstance(response, str):
        return response
    if isinstance(response, BaseModel):
        return response.model_dump_json()
    return json.dumps(response)


class FakeScript:
    """Controls what the fake provider answers.

    Order of resolution for every call: pending failures (``fail_next``), then
    the response queue (``queue``), then the default (``set_default``). With
    none of them the call raises ``FakeScriptExhaustedError`` so a test never
    passes by accident on an unscripted call.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls: list[FakeCall] = []
        self._queue: deque[FakeResponse] = deque()
        self._failures = 0
        self._failure_error: BaseException | None = None
        self._default: FakeResponse | Callable[[FakeCall], FakeResponse] | None = None

    def queue(self, *responses: FakeResponse) -> "FakeScript":
        """Append responses, returned one per call in order.

        A ``str`` is returned as is; a ``dict``/``list``/Pydantic model is
        serialised to JSON; an exception instance is raised.
        """
        with self._lock:
            self._queue.extend(responses)
        return self

    def fail_next(
        self, times: int = 1, error: BaseException | None = None
    ) -> "FakeScript":
        """Make the next ``times`` calls fail (default: retriable provider error)."""
        if times < 0:
            raise ValueError("times must be >= 0")
        with self._lock:
            self._failures = times
            self._failure_error = error
        return self

    def set_default(
        self, response: FakeResponse | Callable[[FakeCall], FakeResponse] | None
    ) -> "FakeScript":
        """Answer used when the queue is empty; may be a function of the call."""
        with self._lock:
            self._default = response
        return self

    def reset(self) -> None:
        """Forget queued responses, failures, default and recorded calls."""
        with self._lock:
            self.calls.clear()
            self._queue.clear()
            self._failures = 0
            self._failure_error = None
            self._default = None

    @property
    def pending(self) -> int:
        """Number of queued responses not yet consumed."""
        return len(self._queue)

    def next_text(self, call: FakeCall) -> str:
        with self._lock:
            self.calls.append(call)
            if self._failures > 0:
                self._failures -= 1
                raise self._failure_error or LLMProviderError(
                    "Scripted fake LLM failure", retriable=True
                )
            if self._queue:
                response: FakeResponse = self._queue.popleft()
            elif self._default is not None:
                default = self._default
                response = default(call) if callable(default) else default
            else:
                raise FakeScriptExhaustedError()
        if isinstance(response, BaseException):
            raise response
        return _to_text(response)


_fake_script = FakeScript()


def get_fake_script() -> FakeScript:
    """The shared script used by every ``fake`` chat model."""
    return _fake_script


def reset_fake_script() -> None:
    _fake_script.reset()


class FakeChatModel(ChatModel):
    """Offline model whose answers come from a :class:`FakeScript`."""

    provider = "fake"

    def __init__(
        self,
        *,
        model: str,
        kind: LLMKind,
        script: FakeScript | None = None,
    ) -> None:
        super().__init__(model=model, kind=kind)
        self.script = script if script is not None else _fake_script

    def _generate(
        self,
        messages: list[Message],
        *,
        temperature: float | None,
        json_schema: dict[str, Any] | None,
    ) -> LLMCompletion:
        call = FakeCall(
            kind=self.kind,
            model=self.model,
            temperature=temperature,
            messages=tuple(messages),
            json_schema=json_schema,
        )
        text = self.script.next_text(call)
        return LLMCompletion(
            text=text,
            tokens_in=sum(len(m.content.split()) for m in messages),
            tokens_out=len(text.split()),
            cost_usd=0.0,
        )


# --------------------------------------------------------------------------
# Provider registry and model selection
# --------------------------------------------------------------------------

ProviderFactory = Callable[[str | None, LLMKind, Settings], ChatModel]


def _build_fake(model: str | None, kind: LLMKind, settings: Settings) -> ChatModel:
    return FakeChatModel(model=model or f"fake-{kind}", kind=kind)


_PROVIDERS: dict[str, ProviderFactory] = {"fake": _build_fake}


def register_provider(name: str, factory: ProviderFactory) -> None:
    """Make ``LLM_PROVIDER=<name>`` available.

    ``factory(model_name, kind, settings)`` gets the configured model name (or
    ``None`` when unset; a real provider should then call
    :func:`require_model_name`) and returns a :class:`ChatModel`.
    """
    _PROVIDERS[name.strip().lower()] = factory


def configured_model_name(kind: LLMKind, settings: Settings) -> str | None:
    """Model name for ``kind`` from settings; ``fast`` falls back to ``text``."""
    text = (settings.LLM_MODEL_TEXT or "").strip() or None
    if kind == "text":
        return text
    return (settings.LLM_MODEL_FAST or "").strip() or text


def require_model_name(model: str | None, kind: LLMKind) -> str:
    """For real providers: fail clearly when no model is configured."""
    if model:
        return model
    setting = "LLM_MODEL_TEXT" if kind == "text" else "LLM_MODEL_FAST or LLM_MODEL_TEXT"
    raise LLMConfigError(f"No LLM model configured: set {setting}")


def get_chat_model(
    kind: LLMKind = "text", *, settings: Settings | None = None
) -> ChatModel:
    """Return the chat model for ``kind`` (``"text"`` strong, ``"fast"`` cheap)."""
    if kind not in _KINDS:
        raise LLMConfigError(f"Unknown LLM kind: {kind!r}", details={"kinds": _KINDS})
    cfg = settings if settings is not None else get_settings()
    provider = (cfg.LLM_PROVIDER or "").strip().lower()
    factory = _PROVIDERS.get(provider)
    if factory is None:
        raise LLMConfigError(
            f"LLM_PROVIDER {provider!r} is not available",
            details={"available": sorted(_PROVIDERS)},
        )
    return factory(configured_model_name(kind, cfg), kind, cfg)


# --------------------------------------------------------------------------
# Structured output
# --------------------------------------------------------------------------

_MAX_EMBEDDED_TRIES = 5


class _OutputProblem(Exception):
    """Internal: the reply cannot be used. ``summary`` never includes the reply."""

    def __init__(self, summary: str) -> None:
        super().__init__(summary)
        self.summary = summary


def extract_json(text: str) -> Any:
    """Parse a JSON value from a model reply.

    Accepts plain JSON, or a JSON object or array embedded in surrounding text
    (which covers replies wrapped in a Markdown code fence). Raises
    ``ValueError`` if none is found.
    """
    candidate = text.strip()
    if not candidate:
        raise ValueError("the reply was empty")
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as first_error:
        decoder = json.JSONDecoder()
        tries = 0
        for index, char in enumerate(candidate):
            if char not in "{[":
                continue
            tries += 1
            if tries > _MAX_EMBEDDED_TRIES:
                break
            try:
                value, _ = decoder.raw_decode(candidate, index)
            except json.JSONDecodeError:
                continue
            return value
        raise ValueError(
            "the reply is not valid JSON "
            f"(line {first_error.lineno}, column {first_error.colno})"
        ) from None


def _describe_validation_error(exc: PydanticValidationError) -> str:
    parts: list[str] = []
    for err in exc.errors(include_input=False, include_url=False)[:_MAX_PROBLEMS]:
        location = ".".join(str(p) for p in err["loc"]) or "(root)"
        parts.append(f"{location}: {err['msg']}")
    return "; ".join(parts)


def _parse(schema: type[T], text: str) -> T:
    try:
        data = extract_json(text)
    except ValueError as exc:
        raise _OutputProblem(str(exc)) from None
    try:
        return schema.model_validate(data)
    except PydanticValidationError as exc:
        raise _OutputProblem(_describe_validation_error(exc)) from None


def _repair_messages(
    original: Sequence[Message],
    bad_reply: str,
    problem: str,
    json_schema: dict[str, Any],
) -> list[Message]:
    return [
        *original,
        Message("assistant", bad_reply[:_REPAIR_ECHO_LIMIT]),
        Message(
            "user",
            "Your previous reply could not be used: "
            f"{problem}.\n"
            "Reply again with ONLY one JSON value that matches this JSON schema. "
            "No commentary and no code fences.\n"
            f"JSON schema: {json.dumps(json_schema)}",
        ),
    ]


def generate_structured(
    schema: type[T],
    messages: Sequence[Message],
    *,
    kind: LLMKind = "text",
    temperature: float | None = None,
    model: ChatModel | None = None,
) -> T:
    """Call the model and return a validated ``schema`` instance.

    If the reply is not valid JSON or fails validation, the model is asked once
    more (``MAX_REPAIR_RETRIES``) with the problem described. Provider failures
    (``LLMProviderError``) are not retried here. After the repair attempt fails
    an ``LLMOutputError`` is raised; its message and details never contain the
    model's reply.

    ``model`` overrides ``get_chat_model(kind)`` (mainly for tests).
    """
    original = list(messages)
    if not original:
        raise ValueError("messages must not be empty")

    chat = model if model is not None else get_chat_model(kind)
    json_schema = schema.model_json_schema()
    prompt = original
    problems: list[str] = []

    for attempt in range(1, MAX_REPAIR_RETRIES + 2):
        response = chat.invoke(prompt, temperature=temperature, json_schema=json_schema)
        try:
            return _parse(schema, response.text)
        except _OutputProblem as problem:
            problems.append(problem.summary)
            if attempt > MAX_REPAIR_RETRIES:
                break
            logger.warning(
                "LLM output unusable, asking for a repair schema=%s attempt=%d",
                schema.__name__,
                attempt,
            )
            prompt = _repair_messages(
                original, response.text, problem.summary, json_schema
            )

    raise LLMOutputError(
        f"LLM output did not match {schema.__name__} after {len(problems)} attempts",
        details={
            "schema": schema.__name__,
            "attempts": len(problems),
            "problems": problems,
        },
    )
