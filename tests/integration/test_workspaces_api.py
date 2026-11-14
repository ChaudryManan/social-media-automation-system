"""Integration tests for the workspaces CRUD API (B1-08)."""

import socket
import threading
import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from urllib.parse import urlparse, urlunparse

import psycopg
import pytest
import sqlalchemy as sa
from app.core.config import settings
from app.core.db import get_db
from app.core.errors import LimitExceededError
from app.core.security import create_access_token
from app.main import app
from app.models import Base
from app.models.audit import AuditLog
from app.models.limits import UserLimits
from app.models.settings import SystemSettings
from app.models.users import User
from app.models.workspaces import Workspace
from app.schemas.workspaces import WorkspaceCreate
from app.services.workspaces import WorkspaceService
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker


def _get_test_db_url() -> str:
    url = settings.TEST_DATABASE_URL
    parsed = urlparse(url)
    if parsed.port == 5432 and parsed.hostname in ("localhost", "127.0.0.1", ""):
        raise RuntimeError(
            "Safety error: Refusing to connect to port 5432 on localhost."
        )
    return url


def _postgres_is_reachable(test_db_url: str) -> bool:
    parsed = urlparse(test_db_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 15432
    try:
        sock = socket.create_connection((host, port), timeout=1)
        sock.close()
    except OSError:
        return False

    admin_url = urlunparse(parsed._replace(scheme="postgresql", path="/postgres"))
    try:
        with psycopg.connect(admin_url, connect_timeout=2, autocommit=True) as conn:
            conn.execute("SELECT 1")
            return True
    except (OSError, psycopg.Error):
        return False


def _ensure_test_database() -> None:
    parsed = urlparse(_test_url)
    admin_url = urlunparse(parsed._replace(scheme="postgresql", path="/postgres"))
    with psycopg.connect(admin_url, autocommit=True) as admin_conn:
        exists = admin_conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = 'sma_test'"
        ).fetchone()
        if not exists:
            admin_conn.execute("CREATE DATABASE sma_test OWNER sma")


_test_url = _get_test_db_url()
_reachable = _postgres_is_reachable(_test_url)

if _reachable:
    _ensure_test_database()
    _test_engine = sa.create_engine(_test_url, poolclass=sa.pool.NullPool)
    _TestingSessionLocal = sessionmaker(
        autocommit=False, autoflush=False, bind=_test_engine
    )
    Base.metadata.create_all(bind=_test_engine)
else:
    _test_engine = None
    _TestingSessionLocal = None


@pytest.fixture
def db() -> Generator[Session, None, None]:
    """Provide a database session and truncate all tables after each test."""
    if not _reachable or _test_engine is None or _TestingSessionLocal is None:
        pytest.skip("PostgreSQL is not reachable; skipping integration test.")

    _ensure_test_database()
    Base.metadata.create_all(bind=_test_engine)

    session = _TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        with _test_engine.begin() as conn:
            conn.execute(
                sa.text(
                    "TRUNCATE TABLE users, refresh_tokens, user_limits, "
                    "workspaces, system_settings, audit_logs CASCADE;"
                )
            )


@pytest.fixture
def client(db: Session) -> Generator[TestClient, None, None]:
    """FastAPI TestClient whose get_db dependency uses the test session."""

    def _override_get_db() -> Generator[Session, None, None]:
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _make_user(
    db: Session, email: str, role: str = "user"
) -> tuple[User, dict[str, str]]:
    """Create a user and return it with a Bearer header (no password login)."""
    user = User(
        email=email,
        password_hash="not-a-real-hash",
        role=role,
        is_active=True,
    )
    db.add(user)
    db.commit()
    token = create_access_token(subject=str(user.id), email=user.email, role=user.role)
    return user, {"Authorization": f"Bearer {token}"}


def _set_limit(db: Session, user: User, max_workspaces: int | None) -> None:
    """Give ``user`` a ``user_limits`` override for ``max_workspaces``."""
    db.add(UserLimits(user_id=user.id, max_workspaces=max_workspaces))
    db.commit()


def _audit_rows(db: Session, action: str | None = None) -> list[AuditLog]:
    db.expire_all()
    stmt = select(AuditLog).order_by(AuditLog.created_at, AuditLog.action)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    return list(db.scalars(stmt).all())


def _create(client: TestClient, headers: dict[str, str], **payload: object) -> dict:
    payload.setdefault("name", "Brand A")
    response = client.post("/workspaces", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------- create


@pytest.mark.integration
def test_create_workspace_returns_201_with_defaults(
    client: TestClient, db: Session
) -> None:
    owner, headers = _make_user(db, "owner@example.com")

    response = client.post("/workspaces", json={"name": "  Brand A "}, headers=headers)

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Brand A"
    assert body["owner_id"] == str(owner.id)
    assert body["default_language"] == "en"
    assert body["timezone"] == "UTC"
    assert body["description"] is None
    assert body["default_platforms"] is None
    assert body["is_suspended"] is False
    assert uuid.UUID(body["id"])
    assert body["created_at"]
    assert body["updated_at"]


@pytest.mark.integration
def test_create_workspace_with_all_fields(client: TestClient, db: Session) -> None:
    _, headers = _make_user(db, "owner@example.com")

    body = _create(
        client,
        headers,
        name="Karachi Cafe",
        description="Coffee shop",
        brand_voice="Friendly, short sentences",
        default_language="ur",
        timezone="Asia/Karachi",
        default_platforms=["instagram", "facebook", "instagram"],
        settings={"posting_hours": [9, 18]},
    )

    assert body["description"] == "Coffee shop"
    assert body["brand_voice"] == "Friendly, short sentences"
    assert body["default_language"] == "ur"
    assert body["timezone"] == "Asia/Karachi"
    assert body["default_platforms"] == ["instagram", "facebook"]
    assert body["settings"] == {"posting_hours": [9, 18]}


@pytest.mark.integration
def test_create_requires_authentication(client: TestClient) -> None:
    response = client.post("/workspaces", json={"name": "Brand A"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"


@pytest.mark.integration
@pytest.mark.parametrize(
    "payload",
    [
        {"name": "   "},
        {},
        {"name": "A", "timezone": "Mars/Base"},
        {"name": "A", "default_language": "english language"},
        {"name": "A", "default_platforms": ["myspace"]},
        {"name": "A", "is_suspended": True},
        {"name": "A", "owner_id": str(uuid.uuid4())},
    ],
)
def test_create_rejects_invalid_payloads(
    client: TestClient, db: Session, payload: dict
) -> None:
    _, headers = _make_user(db, "owner@example.com")

    response = client.post("/workspaces", json=payload, headers=headers)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.integration
def test_duplicate_name_is_rejected_ignoring_case(
    client: TestClient, db: Session
) -> None:
    _, headers = _make_user(db, "owner@example.com")
    _create(client, headers, name="Brand A")

    response = client.post("/workspaces", json={"name": "brand a"}, headers=headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "workspace_name_taken"


@pytest.mark.integration
def test_same_name_allowed_for_different_owners(
    client: TestClient, db: Session
) -> None:
    _, headers_a = _make_user(db, "a@example.com")
    _, headers_b = _make_user(db, "b@example.com")

    _create(client, headers_a, name="Brand A")
    response = client.post("/workspaces", json={"name": "Brand A"}, headers=headers_b)

    assert response.status_code == 201


@pytest.mark.integration
def test_name_can_be_reused_after_soft_delete(client: TestClient, db: Session) -> None:
    _, headers = _make_user(db, "owner@example.com")
    first = _create(client, headers, name="Brand A")
    deleted = client.delete(f"/workspaces/{first['id']}?confirm=true", headers=headers)
    assert deleted.status_code == 204

    response = client.post("/workspaces", json={"name": "Brand A"}, headers=headers)

    assert response.status_code == 201
    assert response.json()["id"] != first["id"]


@pytest.mark.integration
def test_concurrent_duplicate_is_mapped_to_conflict(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If two requests race past the name pre-check, the DB index still wins."""
    _, headers = _make_user(db, "owner@example.com")
    _create(client, headers, name="Brand A")
    monkeypatch.setattr(WorkspaceService, "_name_taken", lambda *a, **k: False)

    response = client.post("/workspaces", json={"name": "Brand A"}, headers=headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "workspace_name_taken"
    # The session stays usable after the rollback.
    assert client.get("/workspaces", headers=headers).status_code == 200


# ----------------------------------------------------------------------- list


@pytest.mark.integration
def test_list_returns_only_own_live_workspaces_newest_first(
    client: TestClient, db: Session
) -> None:
    _, headers_a = _make_user(db, "a@example.com")
    _, headers_b = _make_user(db, "b@example.com")
    first = _create(client, headers_a, name="First")
    second = _create(client, headers_a, name="Second")
    gone = _create(client, headers_a, name="Gone")
    _create(client, headers_b, name="Someone else")
    client.delete(f"/workspaces/{gone['id']}?confirm=true", headers=headers_a)

    response = client.get("/workspaces", headers=headers_a)

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["limit"] == 20
    assert body["offset"] == 0
    assert [item["id"] for item in body["items"]] == [second["id"], first["id"]]


@pytest.mark.integration
def test_list_pagination(client: TestClient, db: Session) -> None:
    owner, headers = _make_user(db, "owner@example.com")
    _set_limit(db, owner, 10)  # this test needs more than the default 3
    ids = [_create(client, headers, name=f"WS {i}")["id"] for i in range(5)]

    page1 = client.get("/workspaces?limit=2&offset=0", headers=headers).json()
    page2 = client.get("/workspaces?limit=2&offset=2", headers=headers).json()
    page3 = client.get("/workspaces?limit=2&offset=4", headers=headers).json()

    assert page1["total"] == page2["total"] == page3["total"] == 5
    newest_first = list(reversed(ids))
    assert [i["id"] for i in page1["items"]] == newest_first[0:2]
    assert [i["id"] for i in page2["items"]] == newest_first[2:4]
    assert [i["id"] for i in page3["items"]] == newest_first[4:5]


@pytest.mark.integration
@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1", "limit=abc"])
def test_list_rejects_bad_pagination(
    client: TestClient, db: Session, query: str
) -> None:
    _, headers = _make_user(db, "owner@example.com")

    response = client.get(f"/workspaces?{query}", headers=headers)

    assert response.status_code == 422


@pytest.mark.integration
def test_list_requires_authentication(client: TestClient) -> None:
    assert client.get("/workspaces").status_code == 401


# ------------------------------------------------------------------------ get


@pytest.mark.integration
def test_owner_can_get_workspace(client: TestClient, db: Session) -> None:
    _, headers = _make_user(db, "owner@example.com")
    created = _create(client, headers)

    response = client.get(f"/workspaces/{created['id']}", headers=headers)

    assert response.status_code == 200
    assert response.json() == created


@pytest.mark.integration
def test_other_user_gets_404_not_403(client: TestClient, db: Session) -> None:
    """User A cannot read user B's workspace, and cannot tell it exists."""
    _, headers_a = _make_user(db, "a@example.com")
    _, headers_b = _make_user(db, "b@example.com")
    created = _create(client, headers_a)

    other = client.get(f"/workspaces/{created['id']}", headers=headers_b)
    missing = client.get(f"/workspaces/{uuid.uuid4()}", headers=headers_b)

    assert other.status_code == 404
    assert other.json()["error"]["code"] == "workspace_not_found"
    assert other.json() == missing.json()


@pytest.mark.integration
def test_admin_can_access_any_workspace(client: TestClient, db: Session) -> None:
    _, owner_headers = _make_user(db, "owner@example.com")
    _, admin_headers = _make_user(db, "admin@example.com", role="admin")
    created = _create(client, owner_headers)

    response = client.get(f"/workspaces/{created['id']}", headers=admin_headers)

    assert response.status_code == 200
    assert response.json()["id"] == created["id"]
    # An admin's own list still only shows the admin's own workspaces.
    assert client.get("/workspaces", headers=admin_headers).json()["total"] == 0


@pytest.mark.integration
def test_invalid_workspace_id_is_422_and_unauthenticated_is_401(
    client: TestClient, db: Session
) -> None:
    _, headers = _make_user(db, "owner@example.com")

    assert client.get("/workspaces/not-a-uuid", headers=headers).status_code == 422
    unauth = client.get(f"/workspaces/{uuid.uuid4()}")
    assert unauth.status_code == 401


# --------------------------------------------------------------------- update


@pytest.mark.integration
def test_partial_update_changes_only_sent_fields(
    client: TestClient, db: Session
) -> None:
    _, headers = _make_user(db, "owner@example.com")
    created = _create(
        client,
        headers,
        description="Old",
        brand_voice="Formal",
        timezone="Asia/Karachi",
    )

    response = client.patch(
        f"/workspaces/{created['id']}",
        json={"name": "Renamed", "default_platforms": ["youtube"]},
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Renamed"
    assert body["default_platforms"] == ["youtube"]
    assert body["description"] == "Old"
    assert body["brand_voice"] == "Formal"
    assert body["timezone"] == "Asia/Karachi"
    assert body["updated_at"] >= created["updated_at"]
    assert body["created_at"] == created["created_at"]


@pytest.mark.integration
def test_update_can_clear_nullable_fields(client: TestClient, db: Session) -> None:
    _, headers = _make_user(db, "owner@example.com")
    created = _create(
        client,
        headers,
        description="Something",
        default_platforms=["facebook"],
        settings={"a": 1},
    )

    response = client.patch(
        f"/workspaces/{created['id']}",
        json={"description": None, "default_platforms": None, "settings": None},
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["description"] is None
    assert body["default_platforms"] is None
    assert body["settings"] is None


@pytest.mark.integration
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"name": None},
        {"timezone": None},
        {"name": "  "},
        {"timezone": "Mars/Base"},
        {"is_suspended": True},
        {"owner_id": str(uuid.uuid4())},
    ],
)
def test_update_rejects_invalid_payloads(
    client: TestClient, db: Session, payload: dict
) -> None:
    _, headers = _make_user(db, "owner@example.com")
    created = _create(client, headers)

    response = client.patch(
        f"/workspaces/{created['id']}", json=payload, headers=headers
    )

    assert response.status_code == 422
    assert client.get(f"/workspaces/{created['id']}", headers=headers).json() == created


@pytest.mark.integration
def test_rename_to_existing_name_conflicts_but_case_change_of_self_is_ok(
    client: TestClient, db: Session
) -> None:
    _, headers = _make_user(db, "owner@example.com")
    _create(client, headers, name="Alpha")
    beta = _create(client, headers, name="Beta")

    clash = client.patch(
        f"/workspaces/{beta['id']}", json={"name": "ALPHA"}, headers=headers
    )
    own_case = client.patch(
        f"/workspaces/{beta['id']}", json={"name": "BETA"}, headers=headers
    )

    assert clash.status_code == 409
    assert clash.json()["error"]["code"] == "workspace_name_taken"
    assert own_case.status_code == 200
    assert own_case.json()["name"] == "BETA"


@pytest.mark.integration
def test_other_user_cannot_update_workspace(client: TestClient, db: Session) -> None:
    _, headers_a = _make_user(db, "a@example.com")
    _, headers_b = _make_user(db, "b@example.com")
    created = _create(client, headers_a, name="Mine")

    response = client.patch(
        f"/workspaces/{created['id']}", json={"name": "Hacked"}, headers=headers_b
    )

    assert response.status_code == 404
    unchanged = client.get(f"/workspaces/{created['id']}", headers=headers_a).json()
    assert unchanged["name"] == "Mine"


@pytest.mark.integration
def test_admin_can_update_other_users_workspace_within_owner_namespace(
    client: TestClient, db: Session
) -> None:
    owner, owner_headers = _make_user(db, "owner@example.com")
    _, admin_headers = _make_user(db, "admin@example.com", role="admin")
    _create(client, owner_headers, name="Taken")
    target = _create(client, owner_headers, name="Target")
    # The admin owns a workspace with this name: it must not block the update.
    _create(client, admin_headers, name="Admin Own")

    ok = client.patch(
        f"/workspaces/{target['id']}",
        json={"name": "Admin Own"},
        headers=admin_headers,
    )
    clash = client.patch(
        f"/workspaces/{target['id']}",
        json={"name": "taken"},
        headers=admin_headers,
    )

    assert ok.status_code == 200
    assert ok.json()["owner_id"] == str(owner.id)
    assert clash.status_code == 409


# --------------------------------------------------------------------- delete


@pytest.mark.integration
@pytest.mark.parametrize("suffix", ["", "?confirm=false"])
def test_delete_requires_confirm_true(
    client: TestClient, db: Session, suffix: str
) -> None:
    _, headers = _make_user(db, "owner@example.com")
    created = _create(client, headers)

    response = client.delete(f"/workspaces/{created['id']}{suffix}", headers=headers)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "confirmation_required"
    assert (
        client.get(f"/workspaces/{created['id']}", headers=headers).status_code == 200
    )


@pytest.mark.integration
def test_delete_soft_deletes_and_hides_workspace(
    client: TestClient, db: Session
) -> None:
    _, headers = _make_user(db, "owner@example.com")
    created = _create(client, headers)

    response = client.delete(
        f"/workspaces/{created['id']}?confirm=true", headers=headers
    )

    assert response.status_code == 204
    assert response.content == b""
    assert (
        client.get(f"/workspaces/{created['id']}", headers=headers).status_code == 404
    )
    assert client.get("/workspaces", headers=headers).json()["total"] == 0
    assert (
        client.patch(
            f"/workspaces/{created['id']}", json={"name": "X"}, headers=headers
        ).status_code
        == 404
    )
    # Soft delete: the row is still in the database with deleted_at set.
    db.expire_all()
    row = db.get(Workspace, uuid.UUID(created["id"]))
    assert row is not None
    assert row.deleted_at is not None
    assert row.deleted_at <= datetime.now(UTC)
    # A second delete behaves like any missing workspace.
    again = client.delete(f"/workspaces/{created['id']}?confirm=true", headers=headers)
    assert again.status_code == 404


@pytest.mark.integration
def test_other_user_cannot_delete_workspace(client: TestClient, db: Session) -> None:
    _, headers_a = _make_user(db, "a@example.com")
    _, headers_b = _make_user(db, "b@example.com")
    created = _create(client, headers_a)

    response = client.delete(
        f"/workspaces/{created['id']}?confirm=true", headers=headers_b
    )

    assert response.status_code == 404
    assert (
        client.get(f"/workspaces/{created['id']}", headers=headers_a).status_code == 200
    )


@pytest.mark.integration
def test_not_owner_gets_404_even_without_confirm(
    client: TestClient, db: Session
) -> None:
    """Authorization is checked before the confirm flag, so nothing leaks."""
    _, headers_a = _make_user(db, "a@example.com")
    _, headers_b = _make_user(db, "b@example.com")
    created = _create(client, headers_a)

    response = client.delete(f"/workspaces/{created['id']}", headers=headers_b)

    assert response.status_code == 404


@pytest.mark.integration
def test_admin_can_delete_any_workspace(client: TestClient, db: Session) -> None:
    _, owner_headers = _make_user(db, "owner@example.com")
    _, admin_headers = _make_user(db, "admin@example.com", role="admin")
    created = _create(client, owner_headers)

    response = client.delete(
        f"/workspaces/{created['id']}?confirm=true", headers=admin_headers
    )

    assert response.status_code == 204
    assert client.get("/workspaces", headers=owner_headers).json()["total"] == 0


# ------------------------------------------------------------------- mounting


@pytest.mark.integration
def test_routes_are_available_under_api_v1_prefix(
    client: TestClient, db: Session
) -> None:
    _, headers = _make_user(db, "owner@example.com")

    created = client.post("/api/v1/workspaces", json={"name": "V1"}, headers=headers)
    assert created.status_code == 201
    workspace_id = created.json()["id"]

    assert client.get("/api/v1/workspaces", headers=headers).json()["total"] == 1
    assert (
        client.get(f"/api/v1/workspaces/{workspace_id}", headers=headers).status_code
        == 200
    )
    assert (
        client.patch(
            f"/api/v1/workspaces/{workspace_id}", json={"name": "V2"}, headers=headers
        ).status_code
        == 200
    )
    assert (
        client.delete(
            f"/api/v1/workspaces/{workspace_id}?confirm=true", headers=headers
        ).status_code
        == 204
    )


# --------------------------------------------------- limits (B1-09, FR-2a.4)


@pytest.mark.integration
def test_default_limit_blocks_workspace_number_limit_plus_one(
    client: TestClient, db: Session
) -> None:
    _, headers = _make_user(db, "owner@example.com")
    for i in range(3):  # code fallback default is 3 (no system_settings rows)
        _create(client, headers, name=f"WS {i}")

    response = client.post("/workspaces", json={"name": "WS 4"}, headers=headers)

    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "workspace_limit_reached"
    assert "at most 3 workspaces" in error["message"]
    assert error["details"] == {"limit": 3, "current": 3}
    assert client.get("/workspaces", headers=headers).json()["total"] == 3


@pytest.mark.integration
def test_user_limit_override_raises_and_lowers_the_limit(
    client: TestClient, db: Session
) -> None:
    high, high_headers = _make_user(db, "high@example.com")
    low, low_headers = _make_user(db, "low@example.com")
    _set_limit(db, high, 5)
    _set_limit(db, low, 1)

    for i in range(5):
        _create(client, high_headers, name=f"H{i}")
    _create(client, low_headers, name="L0")

    over_high = client.post("/workspaces", json={"name": "H5"}, headers=high_headers)
    over_low = client.post("/workspaces", json={"name": "L1"}, headers=low_headers)
    assert over_high.status_code == over_low.status_code == 403
    assert over_high.json()["error"]["details"] == {"limit": 5, "current": 5}
    assert over_low.json()["error"]["details"] == {"limit": 1, "current": 1}


@pytest.mark.integration
def test_system_setting_default_applies_and_user_override_beats_it(
    client: TestClient, db: Session
) -> None:
    db.add(SystemSettings(key="default_max_workspaces", value=1))
    db.commit()
    plain, plain_headers = _make_user(db, "plain@example.com")
    special, special_headers = _make_user(db, "special@example.com")
    _set_limit(db, special, 2)

    _create(client, plain_headers, name="P0")
    blocked = client.post("/workspaces", json={"name": "P1"}, headers=plain_headers)
    _create(client, special_headers, name="S0")
    _create(client, special_headers, name="S1")
    special_blocked = client.post(
        "/workspaces", json={"name": "S2"}, headers=special_headers
    )

    assert blocked.status_code == 403
    assert blocked.json()["error"]["details"]["limit"] == 1
    assert special_blocked.status_code == 403
    assert special_blocked.json()["error"]["details"]["limit"] == 2


@pytest.mark.integration
def test_limit_zero_blocks_the_first_workspace(client: TestClient, db: Session) -> None:
    user, headers = _make_user(db, "zero@example.com")
    _set_limit(db, user, 0)

    response = client.post("/workspaces", json={"name": "Nope"}, headers=headers)

    assert response.status_code == 403
    assert response.json()["error"]["details"] == {"limit": 0, "current": 0}


@pytest.mark.integration
def test_limit_is_per_user(client: TestClient, db: Session) -> None:
    _, headers_a = _make_user(db, "a@example.com")
    _, headers_b = _make_user(db, "b@example.com")
    for i in range(3):
        _create(client, headers_a, name=f"A{i}")

    assert (
        client.post("/workspaces", json={"name": "A3"}, headers=headers_a).status_code
        == 403
    )
    _create(client, headers_b, name="B0")  # unaffected by A being at the limit


@pytest.mark.integration
def test_deleting_a_workspace_frees_a_slot(client: TestClient, db: Session) -> None:
    user, headers = _make_user(db, "owner@example.com")
    _set_limit(db, user, 1)
    first = _create(client, headers, name="First")
    assert (
        client.post("/workspaces", json={"name": "Second"}, headers=headers).status_code
        == 403
    )

    client.delete(f"/workspaces/{first['id']}?confirm=true", headers=headers)

    _create(client, headers, name="Second")


@pytest.mark.integration
def test_limit_error_is_also_served_under_api_v1(
    client: TestClient, db: Session
) -> None:
    user, headers = _make_user(db, "owner@example.com")
    _set_limit(db, user, 0)

    response = client.post("/api/v1/workspaces", json={"name": "X"}, headers=headers)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "workspace_limit_reached"


@pytest.mark.integration
def test_concurrent_creates_cannot_exceed_the_limit(db: Session) -> None:
    """Two simultaneous creates at limit 1: exactly one wins (row lock on user)."""
    assert _TestingSessionLocal is not None
    owner, _ = _make_user(db, "racer@example.com")
    _set_limit(db, owner, 1)
    owner_id = owner.id
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def attempt(name: str) -> None:
        session = _TestingSessionLocal()
        try:
            user = session.get(User, owner_id)
            barrier.wait(timeout=10)
            WorkspaceService.create(session, user, WorkspaceCreate(name=name))
            outcomes.append("created")
        except LimitExceededError:
            outcomes.append("limit")
        finally:
            session.close()

    threads = [threading.Thread(target=attempt, args=(n,)) for n in ("One", "Two")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(outcomes) == ["created", "limit"]


# ---------------------------------------------------------- audit (B1-09)


@pytest.mark.integration
def test_owner_delete_writes_audit_entry(client: TestClient, db: Session) -> None:
    owner, headers = _make_user(db, "owner@example.com")
    created = _create(client, headers, name="Doomed")

    client.delete(f"/workspaces/{created['id']}?confirm=true", headers=headers)

    rows = _audit_rows(db)
    assert [r.action for r in rows] == ["workspace.delete"]
    row = rows[0]
    assert row.actor_id == owner.id
    assert row.entity_type == "workspace"
    assert row.entity_id == created["id"]
    assert row.meta == {
        "name": "Doomed",
        "owner_id": str(owner.id),
        "by_admin": False,
    }


@pytest.mark.integration
def test_admin_delete_logs_access_and_delete_with_by_admin(
    client: TestClient, db: Session
) -> None:
    owner, owner_headers = _make_user(db, "owner@example.com")
    admin, admin_headers = _make_user(db, "admin@example.com", role="admin")
    created = _create(client, owner_headers, name="Theirs")

    client.delete(f"/workspaces/{created['id']}?confirm=true", headers=admin_headers)

    deleted = _audit_rows(db, "workspace.delete")
    accessed = _audit_rows(db, "workspace.admin_access")
    assert len(deleted) == len(accessed) == 1
    assert deleted[0].actor_id == admin.id
    assert deleted[0].meta["by_admin"] is True
    assert deleted[0].meta["owner_id"] == str(owner.id)
    assert accessed[0].meta["method"] == "DELETE"


@pytest.mark.integration
def test_admin_get_of_other_users_workspace_is_audited(
    client: TestClient, db: Session
) -> None:
    owner, owner_headers = _make_user(db, "owner@example.com")
    admin, admin_headers = _make_user(db, "admin@example.com", role="admin")
    created = _create(client, owner_headers)

    response = client.get(f"/workspaces/{created['id']}", headers=admin_headers)

    assert response.status_code == 200
    rows = _audit_rows(db, "workspace.admin_access")
    assert len(rows) == 1
    row = rows[0]
    assert row.actor_id == admin.id
    assert row.entity_type == "workspace"
    assert row.entity_id == created["id"]
    assert row.meta == {
        "owner_id": str(owner.id),
        "method": "GET",
        "path": f"/workspaces/{created['id']}",
    }
    assert row.ip_address


@pytest.mark.integration
def test_admin_update_of_other_users_workspace_is_audited(
    client: TestClient, db: Session
) -> None:
    _, owner_headers = _make_user(db, "owner@example.com")
    _, admin_headers = _make_user(db, "admin@example.com", role="admin")
    created = _create(client, owner_headers)

    client.patch(
        f"/workspaces/{created['id']}", json={"name": "Renamed"}, headers=admin_headers
    )

    rows = _audit_rows(db, "workspace.admin_access")
    assert [r.meta["method"] for r in rows] == ["PATCH"]


@pytest.mark.integration
def test_no_admin_audit_for_own_workspace_or_regular_users(
    client: TestClient, db: Session
) -> None:
    _, owner_headers = _make_user(db, "owner@example.com")
    _, admin_headers = _make_user(db, "admin@example.com", role="admin")
    _, stranger_headers = _make_user(db, "stranger@example.com")
    own = _create(client, owner_headers, name="Mine")
    admin_own = _create(client, admin_headers, name="Admin's own")

    client.get(f"/workspaces/{own['id']}", headers=owner_headers)
    client.get(f"/workspaces/{admin_own['id']}", headers=admin_headers)
    denied = client.get(f"/workspaces/{own['id']}", headers=stranger_headers)

    assert denied.status_code == 404
    assert _audit_rows(db) == []


@pytest.mark.integration
def test_admin_delete_without_confirm_logs_access_but_not_a_delete(
    client: TestClient, db: Session
) -> None:
    _, owner_headers = _make_user(db, "owner@example.com")
    _, admin_headers = _make_user(db, "admin@example.com", role="admin")
    created = _create(client, owner_headers)

    response = client.delete(f"/workspaces/{created['id']}", headers=admin_headers)

    assert response.status_code == 400
    assert len(_audit_rows(db, "workspace.admin_access")) == 1
    assert _audit_rows(db, "workspace.delete") == []
    db.expire_all()
    assert db.get(Workspace, uuid.UUID(created["id"])).deleted_at is None


@pytest.mark.integration
def test_audit_entry_is_rolled_back_if_delete_fails(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The delete audit row and the soft delete are one transaction."""
    owner, _ = _make_user(db, "owner@example.com")
    created = WorkspaceService.create(db, owner, WorkspaceCreate(name="Atomic"))

    def boom() -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(db, "commit", boom)
    with pytest.raises(RuntimeError):
        WorkspaceService.soft_delete(db, created, owner)
    monkeypatch.undo()
    db.rollback()

    assert _audit_rows(db) == []
    db.expire_all()
    assert db.get(Workspace, created.id).deleted_at is None
