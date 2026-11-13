"""Tests for the AppError hierarchy and global exception handlers."""

from app.core.errors import (
    AppError,
    ConflictError,
    ForbiddenError,
    InternalError,
    LimitExceededError,
    NotFoundError,
    UnauthorizedError,
    ValidationError,
    register_error_handlers,
)
from app.core.middleware import RequestIDMiddleware
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel


class SamplePayload(BaseModel):
    name: str
    age: int


def create_test_error_app() -> FastAPI:
    """Create minimal FastAPI app configured with error handlers."""
    test_app = FastAPI()
    test_app.add_middleware(RequestIDMiddleware)
    register_error_handlers(test_app)

    @test_app.get("/raise-custom-app-error")
    def raise_custom_app_error() -> None:
        raise AppError(
            code="custom_code",
            message="A domain error occurred",
            http_status=400,
            details={"field": "topic", "reason": "empty"},
        )

    @test_app.get("/raise-not-found")
    def raise_not_found() -> None:
        raise NotFoundError(
            message="Workspace was not found",
            details={"workspace_id": "abc"},
        )

    @test_app.get("/raise-unauthorized")
    def raise_unauthorized() -> None:
        raise UnauthorizedError(message="Invalid credentials")

    @test_app.get("/raise-forbidden")
    def raise_forbidden() -> None:
        raise ForbiddenError(message="Not permitted to access this workspace")

    @test_app.get("/raise-conflict")
    def raise_conflict() -> None:
        raise ConflictError(message="Workspace name already exists")

    @test_app.get("/raise-validation")
    def raise_validation() -> None:
        raise ValidationError(
            message="Custom validation error",
            details={"param": "too short"},
        )

    @test_app.get("/raise-limit-exceeded")
    def raise_limit_exceeded() -> None:
        raise LimitExceededError(
            message="Workspace quota reached",
            details={"max": 5},
        )

    @test_app.get("/raise-internal-error")
    def raise_internal_error() -> None:
        raise InternalError(message="Something failed internally")

    @test_app.post("/validate-body")
    def validate_body(payload: SamplePayload) -> dict[str, str]:
        return {"status": "ok", "name": payload.name}

    @test_app.get("/raise-unhandled-exception")
    def raise_unhandled() -> None:
        raise RuntimeError("Unexpected boom!")

    return test_app


def test_app_error_response_format() -> None:
    """An endpoint raising AppError returns {error: {code, message, details}}."""
    client = TestClient(create_test_error_app())
    response = client.get("/raise-custom-app-error")

    assert response.status_code == 400
    data = response.json()
    assert "error" in data
    assert data["error"]["code"] == "custom_code"
    assert data["error"]["message"] == "A domain error occurred"
    assert data["error"]["details"] == {"field": "topic", "reason": "empty"}


def test_subclass_errors_return_standard_format() -> None:
    """Specific AppError subclasses must return standard status codes/bodies."""
    client = TestClient(create_test_error_app())

    r1 = client.get("/raise-not-found")
    assert r1.status_code == 404
    assert r1.json()["error"]["code"] == "not_found"
    assert r1.json()["error"]["message"] == "Workspace was not found"
    assert r1.json()["error"]["details"] == {"workspace_id": "abc"}

    r2 = client.get("/raise-unauthorized")
    assert r2.status_code == 401
    assert r2.json()["error"]["code"] == "unauthorized"

    r3 = client.get("/raise-forbidden")
    assert r3.status_code == 403
    assert r3.json()["error"]["code"] == "forbidden"

    r4 = client.get("/raise-conflict")
    assert r4.status_code == 409
    assert r4.json()["error"]["code"] == "conflict"

    r5 = client.get("/raise-validation")
    assert r5.status_code == 422
    assert r5.json()["error"]["code"] == "validation_error"

    r6 = client.get("/raise-limit-exceeded")
    assert r6.status_code == 400
    assert r6.json()["error"]["code"] == "limit_exceeded"

    r7 = client.get("/raise-internal-error")
    assert r7.status_code == 500
    assert r7.json()["error"]["code"] == "internal_error"


def test_fastapi_validation_error_mapped_to_standard_format() -> None:
    """FastAPI 422 validation errors must map to {error: {code, message, details}}."""
    client = TestClient(create_test_error_app())
    # Send invalid body (missing age, name wrong type)
    response = client.post("/validate-body", json={"name": 123})

    assert response.status_code == 422
    data = response.json()
    assert "error" in data
    assert data["error"]["code"] == "validation_error"
    assert data["error"]["message"] == "Request validation failed"
    assert isinstance(data["error"]["details"], list)
    assert len(data["error"]["details"]) > 0


def test_unhandled_exception_returns_500_standard_format() -> None:
    """Unhandled server errors must return 500 without leaking stack traces."""
    client = TestClient(create_test_error_app(), raise_server_exceptions=False)
    response = client.get("/raise-unhandled-exception")

    assert response.status_code == 500
    data = response.json()
    assert "error" in data
    assert data["error"]["code"] == "internal_error"
    assert data["error"]["message"] == "An unexpected error occurred"
    assert data["error"]["details"] == {}
