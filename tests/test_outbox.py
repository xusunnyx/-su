"""Outbox：异步生成日报、重投递幂等、失败可见。"""

from __future__ import annotations

from sqlalchemy import select

from app.models import OutboxEvent
from app.services.stats import StatsService
from app.workers import dispatch_pending, pending_count
from helpers import create_order, load_tenant, pay, setup_funded_customer


def test_paid_event_is_pending_until_dispatched(client, tenant, state):
    customer = setup_funded_customer(client, tenant["headers"])
    order = create_order(client, tenant["headers"], customer["id"], "120.00", key="ob-1-0001")
    pay(client, tenant["headers"], order["order_no"])

    tenant_obj = load_tenant(state, "acme")
    # 后台线程在测试里是关闭的：事件必须还躺在 outbox 里
    assert pending_count(state) == 1
    assert StatsService(state).daily_reports(tenant_obj) == []

    processed = dispatch_pending(state)
    assert processed == 1
    assert pending_count(state) == 0

    reports = StatsService(state).daily_reports(tenant_obj)
    assert len(reports) == 1
    assert reports[0]["order_count"] == 1
    assert reports[0]["gross_minor"] == 12000


def test_report_rebuild_is_idempotent_on_redelivery(client, tenant, state):
    """至少一次投递 + 重算 = 不会重复计数。"""
    customer = setup_funded_customer(client, tenant["headers"])
    order = create_order(client, tenant["headers"], customer["id"], "88.00", key="ob-2-0001")
    pay(client, tenant["headers"], order["order_no"])
    dispatch_pending(state)

    tenant_obj = load_tenant(state, "acme")
    before = StatsService(state).daily_reports(tenant_obj)

    # 手动把事件退回 PENDING，模拟 MQ 重投
    with state.db.session(tenant_obj) as session:
        event = session.execute(select(OutboxEvent)).scalars().one()
        event.status = "PENDING"
        event.processed_at = None

    dispatch_pending(state)
    after = StatsService(state).daily_reports(tenant_obj)
    assert after == before
    assert after[0]["order_count"] == 1


def test_refund_updates_report_refund_bucket(client, tenant, state):
    customer = setup_funded_customer(client, tenant["headers"])
    order = create_order(client, tenant["headers"], customer["id"], "60.00", key="ob-3-0001")
    pay(client, tenant["headers"], order["order_no"])
    client.post(f"/api/v1/orders/{order['order_no']}/refund", headers=tenant["headers"])
    dispatch_pending(state)

    tenant_obj = load_tenant(state, "acme")
    report = StatsService(state).daily_reports(tenant_obj)[0]
    assert report["gross_minor"] == 6000
    assert report["refund_minor"] == 6000


def test_outbox_event_marks_done(client, tenant, state):
    customer = setup_funded_customer(client, tenant["headers"])
    order = create_order(client, tenant["headers"], customer["id"], "10.00", key="ob-4-0001")
    pay(client, tenant["headers"], order["order_no"])
    dispatch_pending(state)

    tenant_obj = load_tenant(state, "acme")
    with state.db.session(tenant_obj) as session:
        event = session.execute(select(OutboxEvent)).scalars().one()
        assert event.status == "DONE"
        assert event.event_type == "order.paid"
        assert event.processed_at is not None
