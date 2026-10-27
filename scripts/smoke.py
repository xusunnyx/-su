"""10 秒自检：不需要 pytest，不需要启动服务。

    python -m scripts.smoke
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="adflow-smoke-"))
    settings = Settings(isolation="row", data_dir=tmp, worker_enabled=False)
    app = create_app(settings)

    with TestClient(app) as client:
        health = client.get("/healthz")
        assert health.status_code == 200, health.text
        assert health.json()["status"] == "ok"

        tenant = client.post(
            "/api/v1/admin/tenants",
            json={"slug": "smoke", "name": "自检租户"},
            headers={"X-Admin-Token": settings.admin_token},
        )
        assert tenant.status_code == 201, tenant.text
        api_key = tenant.json()["api_key"]
        headers = {"X-API-Key": api_key}

        customer = client.post(
            "/api/v1/customers",
            json={"external_ref": "C-001", "name": "自检客户"},
            headers=headers,
        )
        assert customer.status_code == 201, customer.text
        customer_id = customer.json()["id"]

        deposit = client.post(
            f"/api/v1/customers/{customer_id}/deposit",
            json={"amount": "1000.00"},
            headers=headers,
        )
        assert deposit.status_code == 200, deposit.text
        assert deposit.json()["balance_minor"] == 100000

        order_headers = {**headers, "Idempotency-Key": "smoke-key-0001"}
        first = client.post(
            "/api/v1/orders",
            json={"customer_id": customer_id, "amount": "199.00"},
            headers=order_headers,
        )
        assert first.status_code == 201, first.text
        order_no = first.json()["order_no"]

        replay = client.post(
            "/api/v1/orders",
            json={"customer_id": customer_id, "amount": "199.00"},
            headers=order_headers,
        )
        assert replay.status_code == 201, replay.text
        assert replay.headers.get("X-Idempotent-Replay") == "true"
        assert replay.json()["order_no"] == order_no

        paid = client.post(f"/api/v1/orders/{order_no}/pay", headers=headers)
        assert paid.status_code == 200, paid.text

        ledger = client.get(f"/api/v1/orders/{order_no}/ledger", headers=headers)
        assert ledger.status_code == 200, ledger.text
        assert len(ledger.json()) == 2

        account = client.get(f"/api/v1/customers/{customer_id}/account", headers=headers)
        assert account.json()["consistent"] is True, account.text

        summary = client.get("/api/v1/stats/summary?days=7", headers=headers)
        assert summary.status_code == 200, summary.text
        assert summary.json()["gross_minor"] == 19900

    print("SMOKE OK  ->  建库/鉴权/充值/下单/幂等重放/支付/分录/对账/统计 全部通过")
    print(f"临时数据目录: {tmp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
