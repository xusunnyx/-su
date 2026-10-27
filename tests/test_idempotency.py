"""幂等：重放、指纹冲突、并发同键。"""

from __future__ import annotations

import threading
from decimal import Decimal

from sqlalchemy import func, select

from app.models import Order
from app.schemas import OrderCreate
from app.services.orders import OrderService
from helpers import create_customer, load_tenant, setup_funded_customer


def test_same_key_returns_same_order(client, tenant):
    customer = setup_funded_customer(client, tenant["headers"])
    headers = {**tenant["headers"], "Idempotency-Key": "idem-key-000001"}
    body = {"customer_id": customer["id"], "amount": "120.00"}

    first = client.post("/api/v1/orders", json=body, headers=headers)
    second = client.post("/api/v1/orders", json=body, headers=headers)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["order_no"] == second.json()["order_no"]
    assert first.headers.get("X-Idempotent-Replay") is None
    assert second.headers.get("X-Idempotent-Replay") == "true"


def test_same_key_different_body_is_rejected(client, tenant):
    customer = setup_funded_customer(client, tenant["headers"])
    headers = {**tenant["headers"], "Idempotency-Key": "idem-key-000002"}

    client.post(
        "/api/v1/orders",
        json={"customer_id": customer["id"], "amount": "100.00"},
        headers=headers,
    )
    conflicting = client.post(
        "/api/v1/orders",
        json={"customer_id": customer["id"], "amount": "999.00"},
        headers=headers,
    )

    assert conflicting.status_code == 409
    assert conflicting.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_missing_idempotency_key_is_rejected(client, tenant):
    customer = create_customer(client, tenant["headers"])
    response = client.post(
        "/api/v1/orders",
        json={"customer_id": customer["id"], "amount": "10.00"},
        headers=tenant["headers"],
    )
    assert response.status_code == 422


def test_concurrent_same_key_creates_exactly_one_order(client, tenant, state):
    """6 个线程同时用同一个幂等键下单，最终只能有 1 张订单。"""
    customer = setup_funded_customer(client, tenant["headers"])
    tenant_obj = load_tenant(state, "acme")
    service = OrderService(state)
    payload = OrderCreate(customer_id=customer["id"], amount=Decimal("10.00"))

    outcomes: list = []
    errors: list = []

    def worker() -> None:
        try:
            outcomes.append(
                service.create_order(tenant_obj, payload, "race-key-000001")
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, [repr(e) for e in errors]
    assert len({o.body["order_no"] for o in outcomes}) == 1
    assert len(outcomes) == 6

    with state.db.session(tenant_obj) as session:
        assert session.execute(select(func.count(Order.id))).scalar_one() == 1
