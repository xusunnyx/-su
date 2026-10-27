"""限流：滑动窗口。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def _client(tmp_path: Path, limit: int) -> TestClient:
    settings = Settings(
        data_dir=tmp_path / "data",
        worker_enabled=False,
        rate_limit=limit,
        rate_window=60,
    )
    return TestClient(create_app(settings)), settings


def test_rate_limit_blocks_after_threshold(tmp_path):
    client, settings = _client(tmp_path, limit=3)
    with client:
        tenant = client.post(
            "/api/v1/admin/tenants",
            json={"slug": "acme", "name": "Acme"},
            headers={"X-Admin-Token": settings.admin_token},
        ).json()
        headers = {"X-API-Key": tenant["api_key"]}

        codes = [client.get("/api/v1/orders", headers=headers).status_code for _ in range(4)]
        assert codes[:3] == [200, 200, 200]
        assert codes[3] == 429

        blocked = client.get("/api/v1/orders", headers=headers)
        assert blocked.json()["code"] == "RATE_LIMITED"
        assert blocked.headers.get("Retry-After") == "60"


def test_rate_limit_is_per_tenant(tmp_path):
    client, settings = _client(tmp_path, limit=2)
    with client:
        def make(slug: str) -> dict:
            body = client.post(
                "/api/v1/admin/tenants",
                json={"slug": slug, "name": slug},
                headers={"X-Admin-Token": settings.admin_token},
            ).json()
            return {"X-API-Key": body["api_key"]}

        a, b = make("acme"), make("globex")
        client.get("/api/v1/orders", headers=a)
        client.get("/api/v1/orders", headers=a)
        assert client.get("/api/v1/orders", headers=a).status_code == 429
        # 另一个租户不受影响：限流维度是 (tenant, scope)
        assert client.get("/api/v1/orders", headers=b).status_code == 200
