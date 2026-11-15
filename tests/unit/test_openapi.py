"""OpenAPI contract tests (B1-10): /docs must stay complete and truthful.

These run offline without a database: they inspect the generated spec and make
a few requests that fail before any query is made (401 and 422).
"""

import json
import uuid
from typing import Any

import pytest
from app.main import OPENAPI_TAGS, app
from app.schemas import auth as auth_schemas
from app.schemas import workspaces as workspace_schemas
from app.schemas.errors import ErrorResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel

HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
API_PREFIX = "/api/v1"
ROOT_PATHS_IN_DOCS = {"/health", "/health/ready"}

spec: dict[str, Any] = app.openapi()
SCHEMAS: dict[str, Any] = spec["components"]["schemas"]


def _operations() -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (method, path, op)
        for path, item in spec["paths"].items()
        for method, op in item.items()
        if method in HTTP_METHODS
    ]


OPERATIONS = _operations()
OP_IDS = [f"{m.upper()} {p}" for m, p, _ in OPERATIONS]
API_OPERATIONS = [o for o in OPERATIONS if o[1].startswith(API_PREFIX)]
API_OP_IDS = [f"{m.upper()} {p}" for m, p, _ in API_OPERATIONS]
PROTECTED = [o for o in API_OPERATIONS if o[2].get("security")]
PROTECTED_IDS = [f"{m.upper()} {p}" for m, p, _ in PROTECTED]


def _refs(node: Any) -> set[str]:
    """Collect the component schema names referenced anywhere inside ``node``."""
    found: set[str] = set()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            found.add(ref.rsplit("/", 1)[-1])
        for value in node.values():
            found |= _refs(value)
    elif isinstance(node, list):
        for value in node:
            found |= _refs(value)
    return found


def _models() -> dict[str, type[BaseModel]]:
    registry: dict[str, type[BaseModel]] = {}
    for module in (auth_schemas, workspace_schemas, ErrorResponse.__module__):
        if isinstance(module, str):
            continue
        for value in vars(module).values():
            if isinstance(value, type) and issubclass(value, BaseModel):
                registry[value.__name__] = value
    registry["ErrorResponse"] = ErrorResponse
    return registry


MODELS = _models()


def _concrete(path: str) -> str:
    return path.replace("{workspace_id}", str(uuid.uuid4()))


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


# ----------------------------------------------------------- structure of /docs


def test_openapi_json_is_serialisable_and_served(client: TestClient) -> None:
    assert json.loads(json.dumps(spec))["info"]["title"]
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert response.json()["paths"].keys() == spec["paths"].keys()


def test_docs_and_redoc_pages_are_served(client: TestClient) -> None:
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200


def test_api_description_explains_base_path_auth_and_errors() -> None:
    description = spec["info"]["description"]
    assert "/api/v1" in description
    assert "Authorize" in description
    assert "error" in description


@pytest.mark.parametrize(("method", "path", "op"), OPERATIONS, ids=OP_IDS)
def test_every_route_has_a_declared_tag_and_a_summary(
    method: str, path: str, op: dict[str, Any]
) -> None:
    declared = {tag["name"] for tag in OPENAPI_TAGS}
    assert op.get("tags"), f"{method} {path} has no tag"
    assert set(op["tags"]) <= declared
    assert op.get("summary")


def test_every_declared_tag_has_a_description_and_is_used() -> None:
    used = {tag for _, _, op in OPERATIONS for tag in op.get("tags", [])}
    for tag in OPENAPI_TAGS:
        assert tag["description"]
        assert tag["name"] in used, f"tag {tag['name']} has no routes"


def test_each_route_is_listed_once_under_api_v1() -> None:
    root_paths = {p for _, p, _ in OPERATIONS if not p.startswith(API_PREFIX)}
    assert root_paths == ROOT_PATHS_IN_DOCS
    assert {"/api/v1/auth/login", "/api/v1/workspaces"} <= {p for _, p, _ in OPERATIONS}


@pytest.mark.parametrize("path", ["/auth/me", "/workspaces", "/admin/test"])
def test_hidden_root_aliases_still_route(client: TestClient, path: str) -> None:
    """Root copies are hidden from /docs but must keep working (401, not 404)."""
    assert path not in spec["paths"]
    response = client.get(path)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"


# ------------------------------------------------------------- examples present


def _request_schema_names(op: dict[str, Any]) -> set[str]:
    body = op.get("requestBody")
    return _refs(body) if body else set()


def _success_schema_names(op: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for status, response in op["responses"].items():
        if status.startswith("2"):
            names |= _refs(response)
    return names


@pytest.mark.parametrize(("method", "path", "op"), API_OPERATIONS, ids=API_OP_IDS)
def test_request_and_success_schemas_have_examples(
    method: str, path: str, op: dict[str, Any]
) -> None:
    for name in _request_schema_names(op) | _success_schema_names(op):
        assert SCHEMAS[name].get("examples"), f"{name} (used by {method} {path})"


def _example_cases() -> list[tuple[str, int, dict[str, Any]]]:
    cases = []
    for name, schema in SCHEMAS.items():
        for index, example in enumerate(schema.get("examples", [])):
            cases.append((name, index, example))
    return cases


@pytest.mark.parametrize(
    ("name", "index", "example"),
    _example_cases(),
    ids=[f"{n}[{i}]" for n, i, _ in _example_cases()],
)
def test_schema_examples_validate_against_their_models(
    name: str, index: int, example: dict[str, Any]
) -> None:
    """Examples cannot rot: each must be a valid instance of its own model."""
    MODELS[name].model_validate(example)


# ------------------------------------------------------------ error documentation


def test_default_fastapi_validation_schema_is_not_documented() -> None:
    assert "HTTPValidationError" not in SCHEMAS
    assert "ValidationError" not in SCHEMAS


@pytest.mark.parametrize(("method", "path", "op"), API_OPERATIONS, ids=API_OP_IDS)
def test_documented_errors_use_the_standard_envelope(
    method: str, path: str, op: dict[str, Any]
) -> None:
    for status, response in op["responses"].items():
        if status.startswith("2"):
            continue
        content = response["content"]["application/json"]
        assert _refs(content["schema"]) == {"ErrorResponse"}, (method, path, status)
        example = content["example"]
        ErrorResponse.model_validate(example)
        assert example["error"]["code"]


@pytest.mark.parametrize(("method", "path", "op"), PROTECTED, ids=PROTECTED_IDS)
def test_protected_routes_document_401_and_403(
    method: str, path: str, op: dict[str, Any]
) -> None:
    assert {"401", "403"} <= set(op["responses"])


@pytest.mark.parametrize(("method", "path", "op"), API_OPERATIONS, ids=API_OP_IDS)
def test_routes_with_input_document_422(
    method: str, path: str, op: dict[str, Any]
) -> None:
    has_input = bool(op.get("requestBody")) or any(
        p["in"] in {"query", "path"} for p in op.get("parameters", [])
    )
    if has_input:
        assert "422" in op["responses"]


# --------------------------------------- documented errors match real behaviour


@pytest.mark.parametrize(("method", "path", "op"), PROTECTED, ids=PROTECTED_IDS)
def test_missing_credentials_return_the_documented_401(
    client: TestClient, method: str, path: str, op: dict[str, Any]
) -> None:
    response = client.request(method.upper(), _concrete(path), json={})

    assert response.status_code == 401
    documented = op["responses"]["401"]["content"]["application/json"]["example"]
    body = ErrorResponse.model_validate(response.json())
    assert body.error.code == documented["error"]["code"] == "not_authenticated"


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/api/v1/auth/register", {}),
        ("/api/v1/auth/register", {"email": "not-an-email", "password": "x"}),
        ("/api/v1/auth/login", {"email": "a@b.co"}),
        ("/api/v1/auth/refresh", {}),
        ("/api/v1/auth/forgot-password", {"email": 5}),
        ("/api/v1/auth/reset-password", {"token": "t"}),
    ],
)
def test_validation_errors_match_the_documented_422(
    client: TestClient, path: str, payload: dict[str, Any]
) -> None:
    response = client.post(path, json=payload)

    assert response.status_code == 422
    body = ErrorResponse.model_validate(response.json())
    assert body.error.code == "validation_error"
    assert isinstance(body.error.details, list) and body.error.details
    documented = spec["paths"][path]["post"]["responses"]["422"]["content"]
    assert documented["application/json"]["example"]["error"]["code"] == body.error.code
