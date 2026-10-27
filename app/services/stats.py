"""统计查询：两种截然不同的「高并发读」优化手法，各配一个接口。

* ``/stats/summary``  聚合查询 + cache-aside + 单飞（防击穿）+ 版本号失效
* ``/stats/realtime`` 写路径增量计数（Redis INCRBY），读路径 O(1)

为什么不用一套方案：报表口径要「准确」，看板要「快」，
两者的容错边界不同，混在一起会同时牺牲两边。
"""

from __future__ import annotations

import time
from datetime import timedelta

from sqlalchemy import func, select

from ..models import Customer, DailyReport, Order, utcnow
from ..money import from_minor
from ..state import AppState
from .orders import OPEN_STATUSES


class StatsService: 
    def __init__(self, state: AppState) -> None:
        self.state = state

    # ------------------------------------------------------------------ #
    # 聚合报表（缓存）
    # ------------------------------------------------------------------ #
    def summary(self, tenant, days: int = 7) -> dict: 
        cache = self.state.cache
        currency = self.state.settings.currency
        # 版本号随每次支付自增：老 key 自然失效，无需 SCAN/DEL 批量清理
        version = cache.incr(f"stats:ver:{tenant.slug}", 0, ttl=7 * 86400)
        key = f"stats:summary:{tenant.slug}:v{version}:{days}"

        cached = cache.get(key)
        if cached is not None:
            return {**cached, "cache": "HIT"}

        lock_key = f"{key}:lock" #抢到了锁
        if cache.acquire_lock(lock_key, ttl=5):
            try:
                payload = self._compute_summary(tenant, days, currency)
                cache.set(key, payload, ttl=self.state.settings.cache_ttl)
            finally:
                cache.release_lock(lock_key)
            return {**payload, "cache": "MISS"}

        # 没抢到锁：短暂等待别人回填，而不是把压力原样打给数据库
        for _ in range(3):
            time.sleep(0.05)
            cached = cache.get(key)
            if cached is not None: # 等待别人回填
                return {**cached, "cache": "HIT_WAITED"}
        return {**self._compute_summary(tenant, days, currency), "cache": "MISS_UNLOCKED"} # 没有等到别人回填，直接自己查

    def _compute_summary(self, tenant, days: int, currency: str) -> dict: # 计算报表
        cutoff = utcnow() - timedelta(days=days)
        with self.state.db.session(tenant) as session: # 从数据库查询
            total_count, total_gross = session.execute(
                select(
                    func.count(Order.id),
                    func.coalesce(func.sum(Order.amount_minor), 0),
                ).where(Order.paid_at >= cutoff, Order.status.in_(OPEN_STATUSES))
            ).one()

            top_rows = session.execute(
                select(
                    Order.customer_id,
                    Customer.name,
                    func.count(Order.id).label("cnt"),
                    func.coalesce(func.sum(Order.amount_minor), 0).label("gross"),
                )
                .join(Customer, Customer.id == Order.customer_id)
                .where(Order.paid_at >= cutoff, Order.status.in_(OPEN_STATUSES))
                .group_by(Order.customer_id, Customer.name)
                .order_by(func.sum(Order.amount_minor).desc())
                .limit(5)
            ).all()

            series_rows = session.execute( # 从数据库查询
                select(
                    func.date(Order.paid_at).label("day"),
                    func.count(Order.id).label("cnt"),
                    func.coalesce(func.sum(Order.amount_minor), 0).label("gross"),
                )
                .where(Order.paid_at >= cutoff, Order.status.in_(OPEN_STATUSES))
                .group_by(func.date(Order.paid_at))
                .order_by(func.date(Order.paid_at))
            ).all()

        return {
            "days": days,
            "currency": currency,
            "order_count": int(total_count),
            "gross_minor": int(total_gross),
            "gross": str(from_minor(int(total_gross), currency)),
            "top_customers": [
                {
                    "customer_id": row.customer_id,
                    "name": row.name,
                    "order_count": int(row.cnt),
                    "gross_minor": int(row.gross),
                    "gross": str(from_minor(int(row.gross), currency)),
                }
                for row in top_rows
            ],
            "daily": [
                {
                    "day": str(row.day),
                    "order_count": int(row.cnt),
                    "gross_minor": int(row.gross),
                    "gross": str(from_minor(int(row.gross), currency)),
                }
                for row in series_rows
            ],
            "generated_at": utcnow().isoformat() + "Z",
        }

    # ------------------------------------------------------------------ #
    # 物化报表（异步任务产物）
    # ------------------------------------------------------------------ #
    def daily_reports(self, tenant, days: int = 7) -> list[dict]: # 查询物化报表
        cutoff = (utcnow() - timedelta(days=days)).date()
        with self.state.db.session(tenant) as session: # 从数据库查询
            rows = session.execute(
                select(DailyReport)
                .where(DailyReport.day >= cutoff)
                .order_by(DailyReport.day.desc())
            ).scalars().all()
            return [
                {
                    "day": r.day.isoformat(),
                    "order_count": r.order_count,
                    "gross_minor": r.gross_minor,
                    "gross": str(from_minor(r.gross_minor, self.state.settings.currency)),
                    "refund_minor": r.refund_minor,
                    "refund": str(from_minor(r.refund_minor, self.state.settings.currency)),
                }
                for r in rows
            ]

    # ------------------------------------------------------------------ #
    # 实时看板（写路径计数）
    # ------------------------------------------------------------------ #
    def realtime(self, tenant) -> dict: # 查询实时看板
        cache = self.state.cache # 从缓存查询实时看板
        day = utcnow().date().isoformat()
        key = f"rt:{tenant.slug}:{day}"
        currency = self.state.settings.currency

        gross = cache.get(f"{key}:gross")
        count = cache.get(f"{key}:count")
        source = "counter"
        if gross is None or count is None: # 缓存中没有数据，从数据库查询
            source = "db_fallback"
            with self.state.db.session(tenant) as session: # 从数据库查询
                count, gross = session.execute(
                    select(
                        func.count(Order.id),
                        func.coalesce(func.sum(Order.amount_minor), 0),
                    ).where(
                        Order.paid_at >= utcnow().replace(hour=0, minute=0, second=0, microsecond=0),
                        Order.status.in_(OPEN_STATUSES),
                    )
                ).one()

        return {
            "day": day,
            "gross": str(from_minor(int(gross), currency)),
            "gross_minor": int(gross),
            "order_count": int(count),
            "source": source,
        }

    def cache_stats(self) -> dict:
        return self.state.cache.stats()
