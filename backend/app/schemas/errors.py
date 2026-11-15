"""Schemas that describe the standard error format in OpenAPI.

At runtime errors are produced by ``core/errors.py``; these models exist so
``/docs`` shows exactly what clients receive.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ErrorBody(BaseModel):
    """The ``error`` object inside every error response."""

    code: str = Field(description="Stable machine-readable error code.")
    message: str = Field(description="Human-readable explanation.")
    details: dict[str, Any] | list[Any] = Field(
        default_factory=dict,
        description="Extra context (validation errors, limits, hints).",
    )


class ErrorResponse(BaseModel):
    """Standard error envelope: ``{"error": {"code", "message", "details"}}``."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "error": {
                        "code": "workspace_not_found",
                        "message": "Workspace not found",
                        "details": {},
                    }
                }
            ]
        }
    )

    error: ErrorBody
