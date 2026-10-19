from fastapi.testclient import TestClient

from gateway.main import create_app


def test_healthz() -> None:
    response = TestClient(create_app()).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_unknown_route_uses_error_envelope() -> None:
    response = TestClient(create_app()).get("/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
