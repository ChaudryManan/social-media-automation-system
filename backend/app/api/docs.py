"""Helpers that document error responses in OpenAPI.

Usage in a router::

    @router.get("/{id}", responses=errors(401, WORKSPACE_NOT_FOUND, 422))

Plain ints use the generic defaults below; pass an ``ErrorDoc`` to document the
specific error code a route can return.
"""

from typing import Any, NamedTuple

from app.schemas.errors import ErrorResponse


class ErrorDoc(NamedTuple):
    """One documented error response."""

    status: int
    description: str
    code: str
    message: str
    details: Any = None


DEFAULT_ERRORS: dict[int, ErrorDoc] = {
    401: ErrorDoc(
        401,
        "Missing, invalid or expired credentials "
        "(codes: `not_authenticated`, `invalid_token`, `token_expired`, "
        "`user_not_found`).",
        "not_authenticated",
        "Authentication credentials were not provided",
    ),
    403: ErrorDoc(
        403,
        "The account is inactive (`user_inactive`).",
        "user_inactive",
        "User account is inactive",
    ),
    422: ErrorDoc(
        422,
        "Request validation failed. `details` lists each invalid field.",
        "validation_error",
        "Request validation failed",
        [
            {
                "type": "missing",
                "loc": ["body", "name"],
                "msg": "Field required",
                "input": {},
            }
        ],
    ),
}

WORKSPACE_NOT_FOUND = ErrorDoc(
    404,
    "Workspace does not exist, was deleted, or belongs to another user "
    "(all three look the same on purpose).",
    "workspace_not_found",
    "Workspace not found",
)


def errors(*items: int | ErrorDoc) -> dict[int | str, dict[str, Any]]:
    """Build a FastAPI ``responses=`` mapping for the given error docs."""
    responses: dict[int | str, dict[str, Any]] = {}
    for item in items:
        doc = DEFAULT_ERRORS[item] if isinstance(item, int) else item
        responses[doc.status] = {
            "model": ErrorResponse,
            "description": doc.description,
            "content": {
                "application/json": {
                    "example": {
                        "error": {
                            "code": doc.code,
                            "message": doc.message,
                            "details": doc.details if doc.details is not None else {},
                        }
                    }
                }
            },
        }
    return responses
