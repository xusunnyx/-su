"""统计：缓存命中、版本号失效、实时计数降级。"""

from __future__ import annotations

from app.models import utcnow
from helpers import create_order, pay, setup_funded_customer


def _strip_cache(payload: dict) -> dict:
    return {k: v for k, v in payload.items() if k != "cache"}


def test_summary_is_cached_then_invalidated(client, tenant):
    customer = setup_funded_customer(client, tenant["headers"])
    first_order = create_order(client, tenant["headers"], customer["id"], "100.00", key="s-1-00001")
    pay(client, tenant["headers"], first_order["order_no"])

    first = client.get("/api/v1/stats/summary?days=7", headers=tenant["headers"]).json()
    assert first["cache"] == "MISS"
    assert first["gross_minor"] == 10000
    assert first["order_count"] == 1

    second = client.get("/api/v1/stats/summary?days=7", headers=tenant["headers"]).json()
    assert second["cache"] == "HIT"
    assert _strip_cache(second) == _strip_cache(first)

    # 新支付 -> 版本号 +1 -> 老 key 自然失效，无需 scan/delete
    second_order = create_order(client, tenant["headers"], customer["id"], "50.00", key="s-2-00001")
    pay(client, tenant["headers"], second_order["order_no"])

    third = client.get("/api/v1/stats/summary?days=7", headers=tenant["headers"]).json()
    assert third["cache"] == "MISS"
    assert third["gross_minor"] == 15000


def test_cache_stats_exposes_hit_rate(client, tenant):
    setup_funded_customer(client, tenant["headers"])
    client.get("/api/v1/stats/summary?days=7", headers=tenant["headers"])
    client.get("/api/v1/stats/summary?days=7", headers=tenant["headers"])
    stats = client.get("/api/v1/stats/debug/cache", headers=tenant["headers"]).json()
    assert stats["backend"] == "memory"
    assert stats["hits"] >= 1


def test_realtime_reads_counter_and_falls_back_to_db(client, tenant, state):
    customer = setup_funded_customer(client, tenant["headers"])
    order = create_order(client, tenant["headers"], customer["id"], "30.00", key="rt-1-0001")
    pay(client, tenant["headers"], order["order_no"])

    hot = client.get("/api/v1/stats/realtime", headers=tenant["headers"]).json()
    assert hot["source"] == "counter"
    assert hot["gross_minor"] == 3000
    assert hot["order_count"] == 1

    # 模拟缓存被清空/进程重启：必须能回源到数据库，而不是显示 0
    day = utcnow().date().isoformat()
    state.cache.delete(f"rt:acme:{day}:gross", f"rt:acme:{day}:count")

    cold = client.get("/api/v1/stats/realtime", headers=tenant["headers"]).json()
    assert cold["source"] == "db_fallback"
    assert cold["gross_minor"] == 3000


def test_summary_is_tenant_scoped(client, tenant, tenant_b):
    customer = setup_funded_customer(client, tenant["headers"])
    order = create_order(client, tenant["headers"], customer["id"], "77.00", key="scoped-1")
    pay(client, tenant["headers"], order["order_no"])

    other = client.get("/api/v1/stats/summary?days=7", headers=tenant_b["headers"]).json()
    assert other["gross_minor"] == 0
    assert other["top_customers"] == []
