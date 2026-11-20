"""``validate_input``: the last check before any LLM money is spent.

Rules (``AGENT_SPECS.md`` 3.1): topic 3 to 500 characters and not only
symbols; a supported content type; target accounts that are well formed, not
repeated and able to take that content type; a language tag; a valid
``publish_at``; no banned words. Account state and the generation quota need
tables that arrive in B3, so they are optional hooks (``check_accounts``,
``check_quota``) that B3 fills in; banned words come from the moderation hook
(B5 reads ``moderation_rules``; until then ``no_banned_words`` is the stub).

No LLM is used. Every problem is collected (not only the first) so the user
can fix them all at once. With one or more problems the node sets
``status="failed"`` and writes them to ``errors``; :func:`route_after_validation`
then ends the graph. The messages are meant for the person who submitted the
request: they never name the banned word that matched (that would let people
probe the moderation list) and never contain secrets.

The optional LLM safety classifier (``safety_classifier_enabled``) is not
implemented yet.
"""

import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, Literal

from app.agents.platform_rules import (
    PlatformRules,
    default_platform_rules,
    platform_supports,
)
from app.agents.state import CONTENT_TYPES, PLATFORMS, GraphState

TOPIC_MIN_CHARS = 3
TOPIC_MAX_CHARS = 500
MAX_OPTIONS_CHARS = 2000
_MAX_HOOK_MESSAGE_CHARS = 200
_MAX_TEXT_DEPTH = 5

# A BCP-47 style tag: "en", "pt-BR", "zh-Hant-TW".
_LANGUAGE_TAG = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")

BannedWords = Callable[[], Sequence[str]]
PlatformRulesSource = Callable[[], PlatformRules]
AccountCheck = Callable[[GraphState], Sequence[str]]
QuotaCheck = Callable[[GraphState], "str | None"]


def no_banned_words() -> Sequence[str]:
    """Moderation stub: no rules yet (the ``moderation_rules`` table is B5-08)."""
    return ()


def _clean_hook_message(message: object) -> str:
    text = " ".join(str(message).split())
    if len(text) > _MAX_HOOK_MESSAGE_CHARS:
        text = text[: _MAX_HOOK_MESSAGE_CHARS - 1].rstrip() + "…"
    return text


def _user_texts(state: GraphState) -> list[str]:
    """Texts the user wrote that end up in prompts: topic, brand voice, options."""
    texts: list[str] = []
    for key in ("topic", "brand_voice"):
        value = state.get(key)
        if isinstance(value, str):
            texts.append(value)

    def walk(value: Any, depth: int) -> None:
        if depth > _MAX_TEXT_DEPTH:
            return
        if isinstance(value, str):
            texts.append(value)
        elif isinstance(value, Mapping):
            for item in value.values():
                walk(item, depth + 1)
        elif isinstance(value, list | tuple):
            for item in value:
                walk(item, depth + 1)

    walk(state.get("options"), 0)
    return texts


def contains_banned_word(texts: Sequence[str], banned: Sequence[str]) -> bool:
    """Case-insensitive whole-word (or whole-phrase) match.

    Whole words only, so a banned "ass" does not block "class".
    """
    patterns = [
        re.compile(rf"(?<!\w){re.escape(word.strip().casefold())}(?!\w)")
        for word in banned
        if isinstance(word, str) and word.strip()
    ]
    if not patterns:
        return False
    folded = [text.casefold() for text in texts]
    return any(p.search(text) for p in patterns for text in folded)


def _topic_errors(topic: Any) -> list[str]:
    if topic is None or (isinstance(topic, str) and not topic.strip()):
        return ["Topic is required."]
    if not isinstance(topic, str):
        return ["Topic must be text."]
    text = topic.strip()
    errors: list[str] = []
    if len(text) < TOPIC_MIN_CHARS:
        errors.append(f"Topic must be at least {TOPIC_MIN_CHARS} characters.")
    if len(text) > TOPIC_MAX_CHARS:
        errors.append(f"Topic must be at most {TOPIC_MAX_CHARS} characters.")
    if not any(ch.isalnum() for ch in text):
        errors.append("Topic must contain letters or numbers, not only symbols.")
    return errors


def _accounts_errors(
    accounts: Any, content_type: Any, rules: PlatformRules
) -> list[str]:
    if not isinstance(accounts, list | tuple) or not accounts:
        return ["At least one target account is required."]
    errors: list[str] = []
    seen: set[str] = set()
    unsupported: list[str] = []
    for index, account in enumerate(accounts, start=1):
        account_id = account.get("account_id") if isinstance(account, Mapping) else None
        platform = account.get("platform") if isinstance(account, Mapping) else None
        if not isinstance(account_id, str) or not account_id.strip():
            errors.append(f"Target account {index} has no account id.")
            continue
        if platform not in PLATFORMS:
            errors.append(f"Target account {index} has an unsupported platform.")
            continue
        if account_id in seen:
            errors.append("The same account is selected more than once.")
        seen.add(account_id)
        if content_type in CONTENT_TYPES and not platform_supports(
            rules, platform, content_type
        ):
            unsupported.append(platform)  # repeats are removed in collect_errors
    errors.extend(
        f"{platform} does not support {content_type} content."
        for platform in unsupported
    )
    return errors


def _publish_at_errors(value: Any) -> list[str]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return ["Publish time is required."]
    message = "Publish time must be an ISO-8601 date and time with a time zone."
    if not isinstance(value, str):
        return [message]
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return [message]
    return [] if parsed.tzinfo is not None else [message]


def _options_errors(options: Any) -> list[str]:
    if options is None:
        return []
    if not isinstance(options, Mapping):
        return ["Options must be an object."]
    try:
        size = len(json.dumps(options, ensure_ascii=False))
    except (TypeError, ValueError):
        return ["Options must contain only plain values."]
    if size > MAX_OPTIONS_CHARS:
        return [f"Options must be at most {MAX_OPTIONS_CHARS} characters."]
    return []


def collect_errors(
    state: GraphState,
    *,
    banned_words: Sequence[str] = (),
    platform_rules: PlatformRules | None = None,
) -> list[str]:
    """Every problem with the request in ``state``, in a stable order."""
    rules = default_platform_rules() if platform_rules is None else platform_rules
    content_type = state.get("content_type")
    errors: list[str] = _topic_errors(state.get("topic"))

    if content_type not in CONTENT_TYPES:
        errors.append(f"Content type must be one of: {', '.join(CONTENT_TYPES)}.")
    errors.extend(_accounts_errors(state.get("target_accounts"), content_type, rules))

    language = state.get("language")
    if language is None or (isinstance(language, str) and not language.strip()):
        errors.append("Language is required.")
    elif not isinstance(language, str) or not _LANGUAGE_TAG.match(language.strip()):
        errors.append("Language must be a language tag such as 'en' or 'pt-BR'.")

    errors.extend(_publish_at_errors(state.get("publish_at")))

    brand_voice = state.get("brand_voice")
    if brand_voice is not None and not isinstance(brand_voice, str):
        errors.append("Brand voice must be text.")
    errors.extend(_options_errors(state.get("options")))

    if contains_banned_word(_user_texts(state), banned_words):
        errors.append("The request contains a word that is not allowed.")
    return list(dict.fromkeys(errors))


def make_validate_input_node(
    *,
    banned_words: BannedWords = no_banned_words,
    platform_rules: PlatformRulesSource = default_platform_rules,
    check_accounts: AccountCheck | None = None,
    check_quota: QuotaCheck | None = None,
) -> Callable[[GraphState], dict[str, Any]]:
    """Build the ``validate_input`` node.

    The hooks are called on every run, so rules an admin changes apply to the
    next request:

    * ``banned_words()``: active banned words (moderation).
    * ``platform_rules()``: the ``platform_rules`` setting.
    * ``check_accounts(state)``: extra problems with the target accounts
      (inactive, wrong workspace, cannot post this type); B3.
    * ``check_quota(state)``: a message if the user's generation quota is used
      up, else ``None``; B3.
    """

    def validate_input(state: GraphState) -> dict[str, Any]:
        errors = collect_errors(
            state,
            banned_words=banned_words(),
            platform_rules=platform_rules(),
        )
        # Hooks look up stored data by id, so they only run on a well-formed request.
        if not errors:
            if check_accounts is not None:
                errors.extend(_clean_hook_message(m) for m in check_accounts(state))
            if check_quota is not None:
                message = check_quota(state)
                if message:
                    errors.append(_clean_hook_message(message))
            errors = list(dict.fromkeys(errors))
        if errors:
            return {
                "status": "failed",
                "errors": [*(state.get("errors") or []), *errors],
            }
        return {"status": "generating"}

    return validate_input


def route_after_validation(state: GraphState) -> Literal["ok", "failed"]:
    """Conditional edge after ``validate_input``: end the graph when it failed."""
    return "failed" if state.get("status") == "failed" else "ok"
