"""测试夹具：每个用例一个独立临时库，后台线程关闭（避免与断言竞争）。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        isolation=os.getenv("TEST_ISOLATION", "row"),
        data_dir=tmp_path / "data",
        worker_enabled=False,
        rate_limit=10_000,
        cache_ttl=15,
    )


@pytest.fixture()
def app(settings: Settings):
    return create_app(settings)


@pytest.fixture()
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def state(app):
    return app.state.ctx


@pytest.fixture()
def tenant(client: TestClient, settings: Settings) -> dict:
    response = client.post(
        "/api/v1/admin/tenants",
        json={"slug": "acme", "name": "Acme 广告代理"},
        headers={"X-Admin-Token": settings.admin_token},
    )
    assert response.status_code == 201, response.text
    return {**response.json(), "headers": {"X-API-Key": response.json()["api_key"]}}


@pytest.fixture()
def tenant_b(client: TestClient, settings: Settings) -> dict:
    response = client.post(
        "/api/v1/admin/tenants",
        json={"slug": "globex", "name": "Globex 广告代理"},
        headers={"X-Admin-Token": settings.admin_token},
    )
    assert response.status_code == 201, response.text
    return {**response.json(), "headers": {"X-API-Key": response.json()["api_key"]}}
