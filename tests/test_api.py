from starlette.testclient import TestClient

from app.config import Settings, get_settings
from app.main import app


def test_health_endpoint(client: TestClient):
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["database"] == "connected"


def test_refresh_without_token_returns_401(client: TestClient):
    response = client.post("/admin/refresh")
    assert response.status_code == 401
    assert "Invalid admin token" in response.json()["detail"]


def test_refresh_with_wrong_token_returns_401(client: TestClient):
    response = client.post(
        "/admin/refresh",
        headers={"X-Admin-Token": "wrong-token"},
    )
    assert response.status_code == 401


def test_refresh_with_valid_token_executes_sync(client: TestClient):
    settings = get_settings()
    token = settings.ADMIN_TOKEN or "secret-admin-token"

    response = client.post(
        "/admin/refresh",
        headers={"X-Admin-Token": token},
        params={"force": True},
    )
    assert response.status_code == 200
    summary = response.json()
    assert summary["status"] == "success"
    assert summary["inserted"] == 36

    # Verify runs endpoint
    runs_resp = client.get("/admin/ingestion/runs?limit=5")
    assert runs_resp.status_code == 200
    runs = runs_resp.json()
    assert len(runs) >= 1
    assert runs[0]["id"] == summary["run_id"]

    # Verify errors endpoint for this run
    errors_resp = client.get(f"/admin/ingestion/runs/{summary['run_id']}/errors")
    assert errors_resp.status_code == 200
    assert isinstance(errors_resp.json(), list)


def test_refresh_with_unset_admin_token_returns_503(client: TestClient):
    def override_settings() -> Settings:
        return Settings(ADMIN_TOKEN=None)

    app.dependency_overrides[get_settings] = override_settings
    try:
        response = client.post(
            "/admin/refresh",
            headers={"X-Admin-Token": "any-token"},
        )
        assert response.status_code == 503
        assert "ADMIN_TOKEN is not configured" in response.json()["detail"]
    finally:
        app.dependency_overrides.pop(get_settings, None)
