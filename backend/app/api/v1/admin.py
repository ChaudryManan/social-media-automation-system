"""Platform administration API router."""

from typing import Any

from fastapi import APIRouter, Depends, status

from app.core.deps import require_admin
from app.models.users import User

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get(
    "/test",
    status_code=status.HTTP_200_OK,
    summary="Admin-only dummy verification endpoint",
)
def admin_test(
    current_admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Test endpoint that requires platform administrator privileges."""
    return {
        "status": "ok",
        "message": "Admin authorization verified",
        "admin_id": str(current_admin.id),
        "email": current_admin.email,
        "role": current_admin.role,
    }
