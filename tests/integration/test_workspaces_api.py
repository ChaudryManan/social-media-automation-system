"""Integration tests for the workspaces CRUD API (B1-08)."""

import socket
import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from urllib.parse import urlparse, urlunparse

import psycopg
import pytest
import sqlalchemy as sa
from app.core.config import settings
from app.core.db import get_db
from app.core.security import create_access_token
from app.main import app
from app.models import Base
from app.models.users import User
from app.models.workspaces import Workspace
from app.services.workspaces import WorkspaceService
from fastapi.testclient import TestClient
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
    _, headers = _make_user(db, "owner@example.com")
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
