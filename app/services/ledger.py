"""复式记账。

三条分录类型，借贷永远相等：

===========  ======================  ======================
业务         DEBIT（借）               CREDIT（贷）
===========  ======================  ======================
DEPOSIT      CASH  平台银行户          PREPAID 客户预付
PAYMENT      PREPAID 客户预付         REVENUE 平台收入
REFUND       REVENUE 平台收入         PREPAID 客户预付
===========  ======================  ======================

余额符号（存储为 ``accounts.balance_minor``，仅预付账户维护）::

    PREPAID（负债）: balance = credit - debit   # 客户还能花多少
    CASH   （资产）: balance = debit  - credit
    REVENUE（收入）: balance = credit - debit

对账口径：任意账户余额都应能由分录重算出来，见
:func:`recompute_balance` 与 ``tests/test_ledger.py`` 的不变式断言。
"""

from __future__ import annotations

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..models import Account, LedgerEntry

CASH = "CASH"
PREPAID = "PREPAID"
REVENUE = "REVENUE"
DEBIT = "DEBIT"
CREDIT = "CREDIT"

#: 业务类型
DEPOSIT = "DEPOSIT"
PAYMENT = "PAYMENT"
REFUND = "REFUND"


def post_entry( 
    session: Session,
    *,
    tenant_id: int,
    order_id: int | None,
    customer_id: int | None,
    account_code: str,
    direction: str,
    amount_minor: int,
    currency: str,
    biz_type: str,
) -> LedgerEntry:
    if amount_minor <= 0:
        raise ValueError("分录金额必须为正")
    if direction not in (DEBIT, CREDIT):
        raise ValueError(f"非法方向: {direction}")
    entry = LedgerEntry( 
        tenant_id=tenant_id,
        order_id=order_id,
        customer_id=customer_id,
        account_code=account_code,
        direction=direction,
        amount_minor=amount_minor,
        currency=currency,
        biz_type=biz_type,
    )
    session.add(entry)
    return entry


def post_double_entry( 
    session: Session,
    *,
    tenant_id: int,
    order_id: int | None,
    customer_id: int | None,
    debit_account: str,
    credit_account: str,
    amount_minor: int,
    currency: str,
    biz_type: str,
) -> None:
    post_entry(
        session,
        tenant_id=tenant_id,
        order_id=order_id,
        customer_id=customer_id,
        account_code=debit_account,
        direction=DEBIT,
        amount_minor=amount_minor,
        currency=currency,
        biz_type=biz_type,
    )
    post_entry(
        session,
        tenant_id=tenant_id,
        order_id=order_id,
        customer_id=customer_id,
        account_code=credit_account,
        direction=CREDIT,
        amount_minor=amount_minor,
        currency=currency,
        biz_type=biz_type,
    )


def recompute_balance( 
    session: Session, *, account_code: str, customer_id: int | None, currency: str
) -> int:
    """由分录重算账户余额（对账用，不做缓存）。"""
    signed = case(
        (LedgerEntry.direction == CREDIT, LedgerEntry.amount_minor),
        else_=-LedgerEntry.amount_minor,
    )
    total = session.execute(
        select(func.coalesce(func.sum(signed), 0)).where(
            LedgerEntry.account_code == account_code,
            LedgerEntry.currency == currency,
            LedgerEntry.customer_id.is_(None)
            if customer_id is None
            else LedgerEntry.customer_id == customer_id,
        )
    ).scalar_one()
    return int(total)


def order_debit_credit(session: Session, order_id: int) -> tuple[int, int]:
    """返回某订单的 (借方合计, 贷方合计)，两者必须相等。"""
    debit = session.execute(
        select(func.coalesce(func.sum(LedgerEntry.amount_minor), 0)).where(
            LedgerEntry.order_id == order_id, LedgerEntry.direction == DEBIT
        )
    ).scalar_one()
    credit = session.execute(
        select(func.coalesce(func.sum(LedgerEntry.amount_minor), 0)).where(
            LedgerEntry.order_id == order_id, LedgerEntry.direction == CREDIT
        )
    ).scalar_one()
    return int(debit), int(credit)


def ensure_account( 
    session: Session, *, tenant_id: int, customer_id: int, currency: str
) -> Account:
    account = session.execute(
        select(Account).where(
            Account.customer_id == customer_id, Account.currency == currency
        )
    ).scalar_one_or_none()
    if account is None:
        account = Account(
            tenant_id=tenant_id,
            customer_id=customer_id,
            currency=currency,
            balance_minor=0,
        )
        session.add(account)
        session.flush()
    return account
