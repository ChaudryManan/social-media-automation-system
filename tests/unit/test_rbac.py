"""Unit tests for Role-Based Access Control (RBAC) dependencies."""

import uuid

import pytest
from app.core.deps import require_admin
from app.core.errors import ForbiddenError
from app.models.users import User


def test_require_admin_allows_admin_role() -> None:
    """require_admin should successfully return the user when role is admin."""
    admin_user = User(
        id=uuid.uuid4(),
        email="admin@example.com",
        password_hash="argon2_secret_hash",
        role="admin",
        is_active=True,
    )
    result = require_admin(current_user=admin_user)
    assert result == admin_user
    assert result.role == "admin"


def test_require_admin_rejects_regular_user() -> None:
    """require_admin raises ForbiddenError admin_required for normal users."""
    regular_user = User(
        id=uuid.uuid4(),
        email="regular@example.com",
        password_hash="argon2_secret_hash",
        role="user",
        is_active=True,
    )
    with pytest.raises(ForbiddenError) as exc_info:
        require_admin(current_user=regular_user)

    assert exc_info.value.code == "admin_required"
    assert exc_info.value.http_status == 403
    assert "Administrator privileges required" in exc_info.value.message
