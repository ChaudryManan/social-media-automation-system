"""Audit trail helper (DATABASE.md 4.19).

``log`` only adds the row to the caller's session; it does not commit. The
caller commits, so the audit entry and the action it describes are saved
together or not at all.
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.audit import AuditLog
from app.models.users import User

REDACTED = "[redacted]"

# Any ``meta`` key containing one of these words is redacted, so a careless
# caller cannot put secrets into the audit trail (AGENTS.md section 3, rule 6).
_SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "api_key",
    "apikey",
    "credential",
)


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _clean(value: Any) -> Any:
    """Return a JSON-safe copy of ``value`` with sensitive keys redacted."""
    if isinstance(value, dict):
        return {
            str(key): REDACTED if _is_sensitive(str(key)) else _clean(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple | set | frozenset):
        return [_clean(item) for item in value]
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def redact_sensitive(value: Any) -> Any:
    """Return a JSON-safe copy of ``value`` with sensitive keys redacted.

    Public wrapper so other services (e.g. ``runs``) share one list of
    sensitive key names instead of keeping their own.
    """
    return _clean(value)


def log(
    db: Session,
    actor: User | None,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | str | None = None,
    meta: dict[str, Any] | None = None,
    ip_address: str | None = None,
) -> AuditLog:
    """Add an audit entry to ``db`` and return it (the caller commits).

    ``actor`` is ``None`` for system-initiated events. ``action`` is a dotted
    name such as ``workspace.delete``. Sensitive ``meta`` keys are redacted.
    """
    entry = AuditLog(
        actor_id=actor.id if actor is not None else None,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        meta=_clean(meta) if meta is not None else None,
        ip_address=ip_address,
    )
    db.add(entry)
    return entry
