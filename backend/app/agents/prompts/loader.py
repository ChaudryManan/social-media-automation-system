"""Prompt files: loading, validation and rendering (AGENT_SPECS.md 1.3).

Each agent prompt is one Markdown file ``<name>.md`` in this directory::

    ---
    prompt_version: writer.v1
    ---
    # Role
    # Task
    # Inputs
    # Output format
    # Rules
    # Examples        (optional)

* Variables are written ``{{name}}`` (lower-case letters, digits, ``_``).
* ``{{safety_rules}}`` is built in: it is filled from
  ``fragments/safety_rules.md`` so the shared safety rules exist in one place.
* Loading validates the file (front matter, section order, placeholders), so a
  broken prompt fails in tests, not in a production run.
* Rendering is strict (every variable must be given, unknown ones are an
  error) and single-pass: text inside a value, such as a user's topic, is
  never expanded again, so ``{{...}}`` in user input cannot inject anything.
* Retry attempts pass ``feedback=...``; it is appended as a
  ``# Previous attempt feedback`` block (AGENT_SPECS.md 1.5).
* The ``prompt_version`` is what nodes store in
  ``agent_steps.output_summary.prompt_version``.

Prompts contain no secrets and no hard-coded model names.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.agents.llm import Message, system_message, user_message
from app.core.errors import AppError

PROMPTS_DIR = Path(__file__).resolve().parent
FRAGMENTS_DIR_NAME = "fragments"

REQUIRED_SECTIONS: tuple[str, ...] = (
    "Role",
    "Task",
    "Inputs",
    "Output format",
    "Rules",
)
OPTIONAL_SECTIONS: tuple[str, ...] = ("Examples",)
_SECTION_ORDER: tuple[str, ...] = (*REQUIRED_SECTIONS, *OPTIONAL_SECTIONS)

# Sections sent as the system message; the rest (Task, Inputs, feedback) form
# the user message.
SYSTEM_SECTIONS: tuple[str, ...] = ("Role", "Output format", "Rules", "Examples")
USER_SECTIONS: tuple[str, ...] = ("Task", "Inputs")

BUILTIN_VARIABLES: tuple[str, ...] = ("safety_rules",)
RESERVED_VARIABLES: tuple[str, ...] = ("feedback",)

FEEDBACK_HEADING = "Previous attempt feedback"
FEEDBACK_FOOTER = (
    "Fix every problem listed above and keep what was already good. "
    "Reply again in the required output format."
)

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_PLACEHOLDER = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")
_HEADING = re.compile(r"^# (.+?)\s*$")
_VERSION = re.compile(r"^(?P<name>[a-z][a-z0-9_]*)\.v(?P<n>[1-9][0-9]*)$")
_EMPTY_VALUE = "none"


class PromptError(AppError):
    """A prompt file is missing or invalid, or it was rendered incorrectly."""

    def __init__(
        self,
        message: str,
        code: str = "prompt_invalid",
        details: dict[str, Any] | list[Any] | None = None,
    ) -> None:
        super().__init__(code=code, message=message, http_status=500, details=details)


def _to_text(value: Any) -> str:
    """Turn a variable value into prompt text (structured values become JSON)."""
    if value is None:
        return _EMPTY_VALUE
    if isinstance(value, str):
        return value if value.strip() else _EMPTY_VALUE
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, dict | list | tuple):
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return str(value)


def _substitute(text: str, values: dict[str, str]) -> str:
    # One pass over the template only: inserted values are never re-scanned.
    return _PLACEHOLDER.sub(lambda match: values[match.group(1)], text)


@dataclass(frozen=True)
class PromptTemplate:
    """A validated prompt. Build it with :func:`load_prompt`."""

    name: str
    version: str
    sections: tuple[tuple[str, str], ...]
    variables: frozenset[str]
    """Variables the caller must provide (built-ins excluded)."""
    fragments: dict[str, str] = field(default_factory=dict)
    """Values of the built-in variables used by this prompt."""

    def _values(self, values: dict[str, Any]) -> dict[str, str]:
        missing = sorted(self.variables - values.keys())
        unknown = sorted(values.keys() - self.variables)
        if missing or unknown:
            parts = []
            if missing:
                parts.append(f"missing variables: {', '.join(missing)}")
            if unknown:
                parts.append(f"unknown variables: {', '.join(unknown)}")
            raise PromptError(
                f"Cannot render prompt {self.name!r}: {'; '.join(parts)}",
                code="prompt_variables",
                details={"prompt": self.name, "missing": missing, "unknown": unknown},
            )
        return {**self.fragments, **{k: _to_text(v) for k, v in values.items()}}

    def _text(self, titles: Sequence[str], values: dict[str, str]) -> str:
        return "\n\n".join(
            f"# {title}\n{_substitute(body, values)}"
            for title, body in self.sections
            if title in titles
        )

    @staticmethod
    def _feedback_block(feedback: str | None) -> str:
        if feedback is None or not feedback.strip():
            return ""
        return f"# {FEEDBACK_HEADING}\n{feedback.strip()}\n\n{FEEDBACK_FOOTER}"

    def render(self, feedback: str | None = None, **values: Any) -> str:
        """The whole prompt as one text, with the feedback block if given."""
        filled = self._values(values)
        text = self._text(tuple(t for t, _ in self.sections), filled)
        block = self._feedback_block(feedback)
        return f"{text}\n\n{block}" if block else text

    def messages(self, feedback: str | None = None, **values: Any) -> list[Message]:
        """System message (role, output format, rules) + user message (task, inputs)."""
        filled = self._values(values)
        user = self._text(USER_SECTIONS, filled)
        block = self._feedback_block(feedback)
        if block:
            user = f"{user}\n\n{block}"
        return [
            system_message(self._text(SYSTEM_SECTIONS, filled)),
            user_message(user),
        ]


def _fail(name: str, message: str) -> PromptError:
    return PromptError(f"Prompt {name!r}: {message}", details={"prompt": name})


def _split_front_matter(name: str, text: str) -> tuple[dict[str, str], list[str]]:
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise _fail(name, "must start with a '---' front matter block")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise _fail(name, "front matter block is not closed with '---'") from None
    meta: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep or not key.strip() or not value.strip():
            raise _fail(name, f"invalid front matter line {line!r}")
        meta[key.strip()] = value.strip()
    unknown = sorted(set(meta) - {"prompt_version"})
    if unknown:
        raise _fail(name, f"unknown front matter keys: {', '.join(unknown)}")
    if "prompt_version" not in meta:
        raise _fail(name, "front matter must set prompt_version")
    return meta, lines[end + 1 :]


def _split_sections(name: str, lines: list[str]) -> list[tuple[str, str]]:
    sections: list[tuple[str, list[str]]] = []
    in_fence = False
    for line in lines:
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        heading = None if in_fence else _HEADING.match(line)
        if heading:
            sections.append((heading.group(1), []))
        elif sections:
            sections[-1][1].append(line)
        elif line.strip():
            raise _fail(name, "text before the first '# ' section heading")
    if in_fence:
        raise _fail(name, "unclosed code fence")
    return [(title, "\n".join(body).strip()) for title, body in sections]


def _validate_sections(name: str, sections: list[tuple[str, str]]) -> None:
    titles = [title for title, _ in sections]
    if len(set(titles)) != len(titles):
        raise _fail(name, "duplicate section headings")
    unknown = [t for t in titles if t not in _SECTION_ORDER]
    if unknown:
        raise _fail(name, f"unknown sections: {', '.join(unknown)}")
    missing = [t for t in REQUIRED_SECTIONS if t not in titles]
    if missing:
        raise _fail(name, f"missing sections: {', '.join(missing)}")
    expected = [t for t in _SECTION_ORDER if t in titles]
    if titles != expected:
        raise _fail(name, f"sections must be in this order: {', '.join(expected)}")
    empty = [title for title, body in sections if not body]
    if empty:
        raise _fail(name, f"empty sections: {', '.join(empty)}")


def _read_fragment(name: str, directory: Path, variable: str) -> str:
    path = directory / FRAGMENTS_DIR_NAME / f"{variable}.md"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise _fail(name, f"built-in fragment {variable!r} is missing") from None
    if not text or "{{" in text:
        raise _fail(
            name, f"fragment {variable!r} must be non-empty and have no variables"
        )
    return text


@cache
def _load(name: str, directory: str) -> PromptTemplate:
    base = Path(directory)
    if not _NAME.match(name):
        raise PromptError(
            f"Invalid prompt name {name!r}",
            code="prompt_not_found",
            details={"prompt": name},
        )
    path = base / f"{name}.md"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise PromptError(
            f"Prompt {name!r} not found",
            code="prompt_not_found",
            details={"prompt": name},
        ) from None

    meta, body_lines = _split_front_matter(name, raw)
    version = meta["prompt_version"]
    match = _VERSION.match(version)
    if not match or match.group("name") != name:
        raise _fail(name, f"prompt_version must look like '{name}.v1', got {version!r}")

    sections = _split_sections(name, body_lines)
    _validate_sections(name, sections)

    used: set[str] = set()
    for title, body in sections:
        used.update(_PLACEHOLDER.findall(body))
        if "{{" in _PLACEHOLDER.sub("", body):
            raise _fail(name, f"malformed placeholder in section {title!r}")
    reserved = sorted(used & set(RESERVED_VARIABLES))
    if reserved:
        raise _fail(name, f"reserved variable names used: {', '.join(reserved)}")

    builtins = sorted(used & set(BUILTIN_VARIABLES))
    return PromptTemplate(
        name=name,
        version=version,
        sections=tuple(sections),
        variables=frozenset(used - set(BUILTIN_VARIABLES)),
        fragments={v: _read_fragment(name, base, v) for v in builtins},
    )


def load_prompt(name: str, *, directory: Path | None = None) -> PromptTemplate:
    """Load and validate ``<name>.md`` (cached per file location)."""
    return _load(name, str((directory or PROMPTS_DIR).resolve()))


def list_prompts(*, directory: Path | None = None) -> list[str]:
    """Names of all prompt files in ``directory`` (default: the shipped ones)."""
    base = directory or PROMPTS_DIR
    return sorted(p.stem for p in base.glob("*.md"))
