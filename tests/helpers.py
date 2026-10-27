"""测试用的小工具函数（不是 fixture，方便在参数化里组合）。"""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient


def create_customer(client: TestClient, headers: dict, ref: str = "C-001", name: str = "客户") -> dict:
    response = client.post(
        "/api/v1/customers",
        json={"external_ref": ref, "name": name},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def deposit(client: TestClient, headers: dict, customer_id: int, amount: str) -> dict:
    response = client.post(
        f"/api/v1/customers/{customer_id}/deposit",
        json={"amount": amount},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def create_order(
    client: TestClient,
    headers: dict,
    customer_id: int,
    amount: str,
    key: str = "order-key-0001",
    description: str = "Q3 品牌广告投放",
) -> dict:
    response = client.post(
        "/api/v1/orders",
        json={
            "customer_id": customer_id,
            "amount": amount,
            "description": description,
        },
        headers={**headers, "Idempotency-Key": key},
    )
    assert response.status_code == 201, response.text
    return response.json()


def pay(client: TestClient, headers: dict, order_no: str) -> dict:
    response = client.post(f"/api/v1/orders/{order_no}/pay", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def setup_funded_customer(client: TestClient, headers: dict, balance: str = "1000.00") -> dict:
    customer = create_customer(client, headers)
    deposit(client, headers, customer["id"], balance)
    return customer


def to_minor(amount: str) -> int:
    return int(Decimal(amount) * 100)


def load_tenant(state, slug: str):
    """取租户 ORM 对象，供服务层/线程级测试直接调用（绕过 HTTP）。"""
    from sqlalchemy import select

    from app.models import Tenant

    with state.db.session(None) as session:
        return session.execute(select(Tenant).where(Tenant.slug == slug)).scalar_one()
