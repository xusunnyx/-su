"""异步任务：事务性 Outbox + 后台派发线程。

为什么不用「下单成功后直接 ``BackgroundTasks.add_task``」：
    进程在写库成功、发任务之前崩溃 -> 事件永久丢失；
    任务发了但事务回滚 -> 处理了一个不存在的订单。
Outbox 把「业务数据」和「待发事件」放进同一个事务，再由 worker 至少一次投递，
消费者用「重算」而非「累加」来保证幂等，这是资金类系统的标准解。

换成 Celery / Kafka 时，只需替换 :func:`dispatch_pending` 的驱动，
业务代码（下单、支付）一行都不用改。
"""

from __future__ import annotations

import logging
import threading
from datetime import timedelta
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import DailyReport, Order, OutboxEvent, utcnow
from .state import AppState

logger = logging.getLogger(__name__)

Handler = Callable[[Session, OutboxEvent], None]
HANDLERS: dict[str, Handler] = {}
MAX_ATTEMPTS = 5


def handler(event_type: str) -> Callable[[Handler], Handler]: # 注册事件处理函数
    def decorator(fn: Handler) -> Handler:
        HANDLERS[event_type] = fn
        return fn

    return decorator


def rebuild_daily_report(session: Session, tenant_id: int, day) -> None: # 重建每日聚合
    """按天重算聚合。重算 = 幂等；「累加」在重投递时就会翻倍。"""
    start = utcnow().replace(
        year=day.year, month=day.month, day=day.day,
        hour=0, minute=0, second=0, microsecond=0,
    )
    end = start + timedelta(days=1)

    count, gross = session.execute( # 查询已支付订单数量和金额
        select(func.count(Order.id), func.coalesce(func.sum(Order.amount_minor), 0)).where(
            Order.paid_at >= start,
            Order.paid_at < end,
            Order.status.in_(("PAID", "SETTLED", "REFUNDED")),
        )
    ).one()

    refunded = session.execute( # 查询已退款订单金额
        select(func.coalesce(func.sum(Order.amount_minor), 0)).where(
            Order.refunded_at >= start,
            Order.refunded_at < end,
            Order.status == "REFUNDED",
        )
    ).scalar_one()

    report = session.execute( # 查询或创建每日聚合
        select(DailyReport).where(DailyReport.day == day)
    ).scalar_one_or_none()
    if report is None:
        report = DailyReport(tenant_id=tenant_id, day=day)
        session.add(report)

    report.order_count = int(count)
    report.gross_minor = int(gross)
    report.refund_minor = int(refunded)
    report.updated_at = utcnow()


@handler("order.paid") # 处理订单支付事件
def _on_order_paid(session: Session, event: OutboxEvent) -> None:
    order = session.execute(
        select(Order).where(Order.order_no == event.payload.get("order_no"))
    ).scalar_one_or_none()
    if order is not None and order.paid_at is not None:
        rebuild_daily_report(session, event.tenant_id, order.paid_at.date())


@handler("order.refunded") # 处理订单退款事件
def _on_order_refunded(session: Session, event: OutboxEvent) -> None:
    order = session.execute(
        select(Order).where(Order.order_no == event.payload.get("order_no"))
    ).scalar_one_or_none()
    if order is None:
        return
    day = (order.refunded_at or order.paid_at or utcnow()).date()
    rebuild_daily_report(session, event.tenant_id, day)
    if order.paid_at is not None and order.paid_at.date() != day:
        rebuild_daily_report(session, event.tenant_id, order.paid_at.date())


def _handle_one(session: Session, event: OutboxEvent) -> None: # 处理单条事件
    fn = HANDLERS.get(event.event_type)
    if fn is None:
        event.status = "FAILED"
        event.last_error = f"没有注册 {event.event_type} 的处理器"
        return
    try:
        fn(session, event)
        event.status = "DONE"
        event.processed_at = utcnow()
    except Exception as exc:  # noqa: BLE001 - 单条事件失败不能拖垮整批
        event.attempts += 1
        event.last_error = str(exc)[:480]
        backoff = min(60, 2 ** event.attempts)
        event.available_at = utcnow() + timedelta(seconds=backoff)
        if event.attempts >= MAX_ATTEMPTS:
            event.status = "FAILED"
        logger.warning(
            "outbox event %s failed (attempt=%s): %s", event.id, event.attempts, exc
        )


def dispatch_pending(state: AppState, limit: int = 50) -> int:
    """扫描所有租户的待处理事件。返回处理条数（测试可直接调用）。"""
    processed = 0
    for tenant in state.db.list_tenants():
        while processed < limit:
            with state.db.session(tenant) as session:
                event = session.execute(
                    select(OutboxEvent)
                    .where(
                        OutboxEvent.status == "PENDING",
                        OutboxEvent.available_at <= utcnow(),
                    )
                    .order_by(OutboxEvent.id)
                    .limit(1)
                ).scalar_one_or_none()
                if event is None:
                    break
                _handle_one(session, event)
                processed += 1
        if processed >= limit:
            break
    return processed


def pending_count(state: AppState) -> int: # 获取待处理事件数量
    total = 0
    for tenant in state.db.list_tenants():
        with state.db.session(tenant) as session:
            total += int(
                session.execute(
                    select(func.count(OutboxEvent.id)).where(OutboxEvent.status == "PENDING")
                ).scalar_one()
            )
    return total


class OutboxWorker(threading.Thread):
    """单进程 demo 用线程；生产用独立 worker 进程 + 至少一次投递 + 死信队列。"""

    def __init__(self, state: AppState, interval: float = 0.5) -> None: # 初始化线程
        super().__init__(name="outbox-worker", daemon=True)
        self.state = state
        self.interval = interval
        self._stopping = threading.Event()

    def run(self) -> None:  # pragma: no cover - 线程逻辑由集成测试覆盖
        logger.info("outbox worker started (interval=%.2fs)", self.interval)
        while not self._stopping.wait(self.interval):
            try:
                dispatch_pending(self.state)
            except Exception:  # noqa: BLE001
                logger.exception("outbox dispatch failed")

    def stop(self) -> None: # 停止线程
        self._stopping.set()
        self.join(timeout=5)
        logger.info("outbox worker stopped")
