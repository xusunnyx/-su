"""ORM 模型。

几条贯穿全项目的硬约束：

1. **金额只用整数**：``amount_minor`` 存最小货币单位（美分），杜绝浮点误差。
2. **所有租户表都带 ``tenant_id``**：即便 schema 隔离模式也保留该列，
   作为「纵深防御」——库选错了也还有行级兜底。
3. **资金变动必须落复式分录**：``ledger_entries`` 每笔交易借贷相等，
   账户余额可由分录重算校验，这是对账能力的基础。
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime: # 获取当前时间（UTC）
    """统一使用 naive UTC（SQLite 不保存时区），读写两侧语义一致。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------- #
# 控制面：租户自身
# --------------------------------------------------------------------------- #
class Tenant(Base):
    """租户 = 一个广告代理商。api_key 只存哈希，明文只在创建时返回一次。"""

    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# --------------------------------------------------------------------------- #
# 数据面：一律带 tenant_id
# --------------------------------------------------------------------------- #
class Customer(Base):
    """广告主客户（代理商维护的 CRM 档案）。"""

    __tablename__ = "customers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "external_ref", name="uq_customer_tenant_ref"),
        Index("ix_customers_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    external_ref: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    industry: Mapped[str | None] = mapped_column(String(64), default=None)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Account(Base):
    """客户预付资金账户（负债类科目）。余额是「派生值」，由分录维护。"""

    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "customer_id", "currency", name="uq_account"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    balance_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    version: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Order(Base):
    """广告交易订单。状态机：PENDING_PAYMENT -> PAID -> SETTLED/REFUNDED。"""

    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("tenant_id", "order_no", name="uq_order_tenant_no"),
        Index("ix_orders_tenant_created", "tenant_id", "created_at"),
        Index("ix_orders_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    order_no: Mapped[str] = mapped_column(String(48))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(24), default="PENDING_PAYMENT")
    description: Mapped[str | None] = mapped_column(String(256), default=None)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), default=None, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)


class LedgerEntry(Base):
    """复式记账分录。每笔业务至少两条，借贷金额相等。"""

    __tablename__ = "ledger_entries"
    __table_args__ = (
        Index("ix_ledger_tenant_created", "tenant_id", "created_at"),
        Index("ix_ledger_account", "tenant_id", "account_code", "customer_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), default=None, index=True)
    customer_id: Mapped[int | None] = mapped_column(Integer, default=None)
    #: CASH（平台银行户，资产）/ PREPAID（客户预付，负债）/ REVENUE（平台收入）
    account_code: Mapped[str] = mapped_column(String(16))
    direction: Mapped[str] = mapped_column(String(8))  # DEBIT / CREDIT
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    biz_type: Mapped[str] = mapped_column(String(24))  # DEPOSIT / PAYMENT / REFUND
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class IdempotencyRecord(Base):
    """幂等键表。``(tenant_id, key)`` 唯一索引是并发安全的最终保证。"""

    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("tenant_id", "key", name="uq_idempotency_tenant_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="IN_PROGRESS")
    status_code: Mapped[int] = mapped_column(Integer, default=200)
    response_body: Mapped[dict | None] = mapped_column(JSON, default=None)
    resource_id: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)


class OutboxEvent(Base):
    """事务性 outbox：业务写入与事件写入同一个事务，后台线程负责投递。"""

    __tablename__ = "outbox_events"
    __table_args__ = (
        Index("ix_outbox_pending", "status", "available_at", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(512), default=None)
    available_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)


class DailyReport(Base):
    """按天物化聚合（由 outbox 消费者重算生成，天然幂等）。"""

    __tablename__ = "daily_reports"
    __table_args__ = (
        UniqueConstraint("tenant_id", "day", name="uq_report_tenant_day"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    day: Mapped[date] = mapped_column(Date)
    order_count: Mapped[int] = mapped_column(Integer, default=0)
    gross_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    refund_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


# 所有需要租户隔离的模型。db.py 会据此自动注入过滤条件。
TENANT_SCOPED_MODELS = (
    Customer,
    Account,
    Order,
    LedgerEntry,
    IdempotencyRecord,
    OutboxEvent,
    DailyReport,
)
