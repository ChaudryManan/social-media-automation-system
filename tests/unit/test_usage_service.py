"""Unit tests for effective limits and workspace quota checks (B1-09)."""

import uuid
from collections.abc import Generator
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from app.core.errors import LimitExceededError
from app.models import Base
from app.models.limits import UserLimits
from app.models.settings import SystemSettings
from app.models.users import User
from app.models.workspaces import Workspace
from app.services import usage
from app.services.seed import DEFAULT_SYSTEM_SETTINGS
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


def _user(db: Session, email: str = "u@example.com") -> User:
    user = User(email=email, password_hash="x", role="user", is_active=True)
    db.add(user)
    db.commit()
    return user


def _workspace(db: Session, owner: User, name: str, deleted: bool = False) -> Workspace:
    ws = Workspace(
        owner_id=owner.id,
        name=name,
        deleted_at=datetime.now(UTC) if deleted else None,
    )
    db.add(ws)
    db.commit()
    return ws


def _setting(db: Session, key: str, value: object) -> None:
    db.add(SystemSettings(key=key, value=value))
    db.commit()


def test_fallback_defaults_match_seeded_system_settings() -> None:
    seeded = {s["key"]: s["value"] for s in DEFAULT_SYSTEM_SETTINGS}
    for key, value in usage.FALLBACK_DEFAULTS.items():
        assert seeded[key] == value


def test_uses_code_fallback_when_nothing_is_configured(session: Session) -> None:
    user = _user(session)

    limits = usage.get_effective_limits(session, user.id)

    assert limits == usage.EffectiveLimits(3, 5, 100)


def test_system_setting_overrides_code_fallback(session: Session) -> None:
    user = _user(session)
    _setting(session, "default_max_workspaces", 7)

    assert usage.get_effective_limits(session, user.id).max_workspaces == 7


def test_user_override_beats_system_default(session: Session) -> None:
    user = _user(session)
    _setting(session, "default_max_workspaces", 7)
    session.add(UserLimits(user_id=user.id, max_workspaces=2))
    session.commit()

    assert usage.get_effective_limits(session, user.id).max_workspaces == 2


def test_null_user_override_falls_back_to_default_per_field(session: Session) -> None:
    user = _user(session)
    _setting(session, "default_max_workspaces", 7)
    _setting(session, "default_max_accounts_per_workspace", 9)
    session.add(
        UserLimits(user_id=user.id, max_workspaces=None, max_accounts_per_workspace=4)
    )
    session.commit()

    limits = usage.get_effective_limits(session, user.id)

    assert limits.max_workspaces == 7
    assert limits.max_accounts_per_workspace == 4
    assert limits.monthly_generation_limit == 100


def test_zero_override_is_respected_not_treated_as_missing(session: Session) -> None:
    user = _user(session)
    session.add(UserLimits(user_id=user.id, max_workspaces=0))
    session.commit()

    assert usage.get_effective_limits(session, user.id).max_workspaces == 0
    with pytest.raises(LimitExceededError):
        usage.ensure_can_create_workspace(session, user.id)


@pytest.mark.parametrize("bad", ["ten", -1, True, None, 2.5, [3]])
def test_invalid_system_setting_value_uses_code_fallback(
    session: Session, bad: object
) -> None:
    user = _user(session)
    _setting(session, "default_max_workspaces", bad)

    assert usage.get_effective_limits(session, user.id).max_workspaces == 3


def test_negative_user_override_is_ignored(session: Session) -> None:
    user = _user(session)
    session.add(UserLimits(user_id=user.id, max_workspaces=-5))
    session.commit()

    assert usage.get_effective_limits(session, user.id).max_workspaces == 3


def test_count_ignores_deleted_and_other_users_workspaces(session: Session) -> None:
    me = _user(session, "me@example.com")
    other = _user(session, "other@example.com")
    _workspace(session, me, "A")
    _workspace(session, me, "B", deleted=True)
    _workspace(session, other, "C")

    assert usage.count_workspaces(session, me.id) == 1


def test_ensure_allows_up_to_limit_and_blocks_the_next(session: Session) -> None:
    user = _user(session)
    session.add(UserLimits(user_id=user.id, max_workspaces=2))
    session.commit()

    usage.ensure_can_create_workspace(session, user.id)  # 0 of 2
    _workspace(session, user, "One")
    usage.ensure_can_create_workspace(session, user.id)  # 1 of 2
    _workspace(session, user, "Two")

    with pytest.raises(LimitExceededError) as exc_info:
        usage.ensure_can_create_workspace(session, user.id)  # would be #3

    err = exc_info.value
    assert err.code == "workspace_limit_reached"
    assert err.http_status == 403
    assert err.details == {"limit": 2, "current": 2}
    assert "at most 2 workspaces" in err.message


def test_deleting_a_workspace_frees_a_slot(session: Session) -> None:
    user = _user(session)
    session.add(UserLimits(user_id=user.id, max_workspaces=1))
    session.commit()
    ws = _workspace(session, user, "Only")
    with pytest.raises(LimitExceededError):
        usage.ensure_can_create_workspace(session, user.id)

    ws.deleted_at = datetime.now(UTC)
    session.commit()

    usage.ensure_can_create_workspace(session, user.id)


def test_message_uses_singular_for_limit_of_one(session: Session) -> None:
    user = _user(session)
    session.add(UserLimits(user_id=user.id, max_workspaces=1))
    session.commit()
    _workspace(session, user, "Only")

    with pytest.raises(LimitExceededError) as exc_info:
        usage.ensure_can_create_workspace(session, user.id)

    assert "at most 1 workspace." in exc_info.value.message


def test_unknown_user_gets_defaults(session: Session) -> None:
    assert usage.get_effective_limits(session, uuid.uuid4()).max_workspaces == 3
