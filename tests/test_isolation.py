"""多租户隔离：越权读、越权写、ORM 自动过滤。"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.errors import TenantViolation
from app.models import Customer, Order
from helpers import create_customer, create_order, load_tenant, setup_funded_customer


def test_other_tenant_cannot_read_order(client, tenant, tenant_b):
    customer = setup_funded_customer(client, tenant["headers"])
    order = create_order(client, tenant["headers"], customer["id"], "50.00")
    order_no = order["order_no"]

    response = client.get(f"/api/v1/orders/{order_no}", headers=tenant_b["headers"])
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


def test_list_is_scoped(client, tenant, tenant_b):
    customer = setup_funded_customer(client, tenant["headers"])
    create_order(client, tenant["headers"], customer["id"], "50.00")

    own = client.get("/api/v1/orders", headers=tenant["headers"]).json()
    other = client.get("/api/v1/orders", headers=tenant_b["headers"]).json()

    assert len(own["items"]) == 1
    assert other["items"] == []


def test_other_tenant_cannot_read_customer(client, tenant, tenant_b):
    customer = create_customer(client, tenant["headers"], ref="ACME-1")
    response = client.get(
        f"/api/v1/customers/{customer['id']}", headers=tenant_b["headers"]
    )
    assert response.status_code == 404


def test_orm_filters_automatically(client, tenant, tenant_b, state):
    """业务代码不写 where tenant_id，也查不到别的租户的数据。"""
    customer = setup_funded_customer(client, tenant["headers"])
    create_order(client, tenant["headers"], customer["id"], "50.00")

    acme = load_tenant(state, "acme")
    globex = load_tenant(state, "globex")

    with state.db.session(acme) as session:
        assert session.execute(select(func.count(Order.id))).scalar_one() == 1
        # 连「按 id 精确查询」也被兜住
        assert session.execute(select(Order)).scalars().all()[0].tenant_id == acme.id

    with state.db.session(globex) as session:
        assert session.execute(select(func.count(Order.id))).scalar_one() == 0
        assert session.execute(select(Order)).scalars().all() == []


def test_cross_tenant_write_is_rejected(state, tenant, tenant_b):
    """before_flush 守卫：把 A 租户的会话拿去写 B 租户的数据必须报错。"""
    acme = load_tenant(state, "acme")
    globex = load_tenant(state, "globex")

    with pytest.raises(TenantViolation):
        with state.db.session(acme) as session:
            session.add(
                Customer(
                    tenant_id=globex.id,
                    external_ref="HACK-1",
                    name="越权写入",
                )
            )


def test_customer_ref_unique_per_tenant(client, tenant, tenant_b):
    """唯一约束是「租户内唯一」，不同租户可以重名。"""
    create_customer(client, tenant["headers"], ref="SAME-REF")
    create_customer(client, tenant_b["headers"], ref="SAME-REF")

    duplicated = client.post(
        "/api/v1/customers",
        json={"external_ref": "SAME-REF", "name": "重复"},
        headers=tenant["headers"],
    )
    assert duplicated.status_code == 409
    assert duplicated.json()["code"] == "CUSTOMER_EXISTS"
