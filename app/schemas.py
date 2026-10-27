"""API 契约（Pydantic v2）+ 输出序列化。

对外金额给两个字段：``amount``（十进制字符串，给人看）与
``amount_minor``（整数，给机器算）。永远不要只给浮点数。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

from .models import (
    Account,
    Customer,
    DailyReport,
    LedgerEntry,
    Order,
)
from .money import from_minor


# --------------------------------------------------------------------------- #
# 租户
# --------------------------------------------------------------------------- #
class TenantCreate(BaseModel):  # 租户创建模型
    slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=128)


class TenantOut(BaseModel):  # 租户输出模型
    id: int
    slug: str
    name: str
    status: str
    created_at: datetime


class TenantCreated(TenantOut):  # 租户创建输出模型
    """只有创建时返回一次明文 api_key。"""

    api_key: str


# --------------------------------------------------------------------------- #
# 客户
# --------------------------------------------------------------------------- #
class CustomerCreate(BaseModel):  # 客户创建模型
    external_ref: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    industry: str | None = Field(default=None, max_length=64)


class CustomerOut(BaseModel):  # 客户输出模型
    id: int
    external_ref: str
    name: str
    industry: str | None
    status: str
    created_at: datetime


# --------------------------------------------------------------------------- #
# 账户 / 存款
# --------------------------------------------------------------------------- #
class DepositRequest(BaseModel):  # 存款请求模型
    amount: Decimal = Field(gt=0, description="正数金额，如 1000.00")
    currency: str | None = Field(default=None, max_length=8)


class AccountOut(BaseModel):  # 账户输出模型
    customer_id: int
    currency: str
    balance_minor: int
    balance: str
    version: int


class AccountDetail(AccountOut):  # 账户详情输出模型
    """带对账结果的账户视图：存储余额 vs 分录重算余额。"""

    recomputed_balance_minor: int
    consistent: bool


# --------------------------------------------------------------------------- #
# 订单
# --------------------------------------------------------------------------- #
class OrderCreate(BaseModel):  # 订单创建模型
    customer_id: int
    amount: Decimal = Field(gt=0, description="订单金额，单位：元（按币种精度）")
    currency: str | None = Field(default=None, max_length=8)
    description: str | None = Field(default=None, max_length=256)

    @field_validator("currency")
    @classmethod
    def _upper(cls, v: str | None) -> str | None:
        return v.upper() if v else v


class OrderOut(BaseModel):  # 订单输出模型
    id: int
    order_no: str
    customer_id: int
    status: str
    currency: str
    amount: str
    amount_minor: int
    description: str | None
    created_at: datetime
    paid_at: datetime | None
    refunded_at: datetime | None


class LedgerEntryOut(BaseModel):  # 分录输出模型
    id: int
    order_id: int | None
    customer_id: int | None
    account_code: str
    direction: str
    amount: str
    amount_minor: int
    currency: str
    biz_type: str
    created_at: datetime


class DailyReportOut(BaseModel):  # 日报输出模型
    day: str
    order_count: int
    gross: str
    gross_minor: int
    refund: str
    refund_minor: int


# --------------------------------------------------------------------------- #
# 序列化器
# --------------------------------------------------------------------------- #
def order_out(order: Order) -> dict:  # 订单输出模型
    return {
        "id": order.id,
        "order_no": order.order_no,
        "customer_id": order.customer_id,
        "status": order.status,
        "currency": order.currency,
        "amount": str(from_minor(order.amount_minor, order.currency)),
        "amount_minor": order.amount_minor,
        "description": order.description,
        "created_at": order.created_at,
        "paid_at": order.paid_at,
        "refunded_at": order.refunded_at,
    }


def customer_out(customer: Customer) -> dict:  # 客户输出模型
    return {
        "id": customer.id,
        "external_ref": customer.external_ref,
        "name": customer.name,
        "industry": customer.industry,
        "status": customer.status,
        "created_at": customer.created_at,
    }


def account_out(account: Account) -> dict:  # 账户输出模型
    return {
        "customer_id": account.customer_id,
        "currency": account.currency,
        "balance_minor": account.balance_minor,
        "balance": str(from_minor(account.balance_minor, account.currency)),
        "version": account.version,
    }


def ledger_out(entry: LedgerEntry) -> dict:  # 分录输出模型
    return {
        "id": entry.id,
        "order_id": entry.order_id,
        "customer_id": entry.customer_id,
        "account_code": entry.account_code,
        "direction": entry.direction,
        "amount": str(from_minor(entry.amount_minor, entry.currency)),
        "amount_minor": entry.amount_minor,
        "currency": entry.currency,
        "biz_type": entry.biz_type,
        "created_at": entry.created_at,
    }


def report_out(report: DailyReport) -> dict:  # 日报输出模型
    return {
        "day": report.day.isoformat(),
        "order_count": report.order_count,
        "gross": str(from_minor(report.gross_minor, "USD")),
        "gross_minor": report.gross_minor,
        "refund": str(from_minor(report.refund_minor, "USD")),
        "refund_minor": report.refund_minor,
    }
