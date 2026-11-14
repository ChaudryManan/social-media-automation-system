"""Unit tests for the audit log helper (B1-09)."""

import uuid
from collections.abc import Generator
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from app.models import Base
from app.models.audit import AuditLog
from app.models.users import User
from app.services import audit
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture
def session() -> Generator[Session, None, None]:
    engine = sa.create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


@pytest.fixture
def actor(session: Session) -> User:
    user = User(email="a@example.com", password_hash="x", role="admin", is_active=True)
    session.add(user)
    session.commit()
    return user


def test_log_records_actor_action_entity_and_meta(
    session: Session, actor: User
) -> None:
    entity_id = uuid.uuid4()

    audit.log(
        session,
        actor,
        "workspace.delete",
        "workspace",
        entity_id,
        meta={"name": "Brand"},
        ip_address="203.0.113.7",
    )
    session.commit()

    row = session.scalars(select(AuditLog)).one()
    assert row.actor_id == actor.id
    assert row.action == "workspace.delete"
    assert row.entity_type == "workspace"
    assert row.entity_id == str(entity_id)
    assert row.meta == {"name": "Brand"}
    assert row.ip_address == "203.0.113.7"
    assert row.created_at is not None


def test_system_event_has_no_actor_and_optional_fields_are_none(
    session: Session,
) -> None:
    audit.log(session, None, "settings.update", "system_settings")
    session.commit()

    row = session.scalars(select(AuditLog)).one()
    assert row.actor_id is None
    assert row.entity_id is None
    assert row.meta is None
    assert row.ip_address is None


def test_log_does_not_commit(session: Session, actor: User) -> None:
    audit.log(session, actor, "x.y", "thing", "1")

    session.rollback()

    assert session.scalars(select(AuditLog)).all() == []


def test_sensitive_meta_keys_are_redacted_recursively(
    session: Session, actor: User
) -> None:
    audit.log(
        session,
        actor,
        "user.update",
        "user",
        actor.id,
        meta={
            "password": "hunter2",
            "new_password_hash": "abc",
            "access_token": "t",
            "Authorization": "Bearer t",
            "nested": {"client_secret": "s", "ok": 1},
            "items": [{"api_key": "k", "keep": "v"}],
            "name": "visible",
        },
    )
    session.commit()

    meta = session.scalars(select(AuditLog)).one().meta
    assert meta is not None
    assert meta["password"] == audit.REDACTED
    assert meta["new_password_hash"] == audit.REDACTED
    assert meta["access_token"] == audit.REDACTED
    assert meta["Authorization"] == audit.REDACTED
    assert meta["nested"] == {"client_secret": audit.REDACTED, "ok": 1}
    assert meta["items"] == [{"api_key": audit.REDACTED, "keep": "v"}]
    assert meta["name"] == "visible"
    assert "hunter2" not in str(meta)


def test_meta_values_are_made_json_safe(session: Session, actor: User) -> None:
    owner = uuid.uuid4()
    when = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)

    audit.log(
        session,
        actor,
        "workspace.admin_access",
        "workspace",
        owner,
        meta={"owner_id": owner, "at": when, "ids": (owner,)},
    )
    session.commit()

    meta = session.scalars(select(AuditLog)).one().meta
    assert meta == {
        "owner_id": str(owner),
        "at": when.isoformat(),
        "ids": [str(owner)],
    }


def test_caller_meta_dict_is_not_mutated(session: Session, actor: User) -> None:
    meta = {"password": "p", "name": "n"}

    audit.log(session, actor, "a.b", "thing", "1", meta=meta)

    assert meta == {"password": "p", "name": "n"}
