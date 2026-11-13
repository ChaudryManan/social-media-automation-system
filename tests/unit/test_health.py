from app import main
from fastapi.testclient import TestClient


def test_health_reports_api_liveness() -> None:
    response = TestClient(main.app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_reports_dependencies_as_ready(monkeypatch) -> None:
    monkeypatch.setattr(main, "_database_is_available", lambda: True)
    monkeypatch.setattr(main, "_redis_is_available", lambda: True)

    response = TestClient(main.app).get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {"database": True, "redis": True},
    }


def test_ready_reports_unavailable_dependencies(monkeypatch) -> None:
    monkeypatch.setattr(main, "_database_is_available", lambda: False)
    monkeypatch.setattr(main, "_redis_is_available", lambda: True)

    response = TestClient(main.app).get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"database": False, "redis": True},
    }
