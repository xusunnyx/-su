"""客户与预付账户。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from ..deps import RequestContext, enforce_rate_limit, get_app_state, get_context
from ..errors import conflict, not_found
from ..models import Customer
from ..schemas import (
    AccountDetail,
    AccountOut,
    CustomerCreate,
    CustomerOut,
    DepositRequest,
    account_out,
    customer_out,
)
from ..services.orders import OrderService
from ..state import AppState
from sqlalchemy import select

router = APIRouter(
    prefix="/customers",
    tags=["customers"],
    dependencies=[Depends(get_context), Depends(enforce_rate_limit)],
)


@router.post("", response_model=CustomerOut, status_code=201, summary="创建客户")
def create_customer( # 创建客户路由
    payload: CustomerCreate,
    ctx: Annotated[RequestContext, Depends(get_context)],
    state: Annotated[AppState, Depends(get_app_state)],
) -> dict:
    with state.db.session(ctx.tenant) as session:
        duplicated = session.execute(
            select(Customer).where(Customer.external_ref == payload.external_ref)
        ).scalar_one_or_none()
        if duplicated is not None:
            raise conflict(
                "CUSTOMER_EXISTS", "客户编号已存在", external_ref=payload.external_ref
            )
        customer = Customer(
            tenant_id=ctx.tenant.id,
            external_ref=payload.external_ref,
            name=payload.name,
            industry=payload.industry,
        )
        session.add(customer)
        session.flush()
        return customer_out(customer)


@router.get("", response_model=list[CustomerOut], summary="客户列表（游标分页）")
def list_customers( # 查看客户列表路由
    ctx: Annotated[RequestContext, Depends(get_context)],
    state: Annotated[AppState, Depends(get_app_state)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[int | None, Query(ge=1)] = None,
) -> list[dict]:
    with state.db.session(ctx.tenant) as session:
        stmt = select(Customer).order_by(Customer.id.desc()).limit(limit)
        if cursor:
            stmt = stmt.where(Customer.id < cursor)
        rows = session.execute(stmt).scalars().all()
        return [customer_out(c) for c in rows]


@router.get("/{customer_id}", response_model=CustomerOut, summary="客户详情")
def get_customer( # 查看客户详情路由
    customer_id: int,
    ctx: Annotated[RequestContext, Depends(get_context)],
    state: Annotated[AppState, Depends(get_app_state)],
) -> dict:
    with state.db.session(ctx.tenant) as session:
        customer = session.execute(
            select(Customer).where(Customer.id == customer_id)
        ).scalar_one_or_none()
        if customer is None:
            raise not_found("客户", customer_id)
        return customer_out(customer)


@router.post( # 客户预付金入账路由
    "/{customer_id}/deposit",
    response_model=AccountOut,
    summary="客户预付金入账（借 CASH / 贷 PREPAID）",
)
def deposit(
    customer_id: int,
    payload: DepositRequest,
    ctx: Annotated[RequestContext, Depends(get_context)],
    state: Annotated[AppState, Depends(get_app_state)],
) -> dict:
    return OrderService(state).deposit(
        ctx.tenant, customer_id, payload.amount, payload.currency
    )


@router.get( # 查看预付账户余额路由
    "/{customer_id}/account",
    response_model=AccountDetail,
    summary="查看预付账户余额（附对账校验）",
)
def get_account(
    customer_id: int,
    ctx: Annotated[RequestContext, Depends(get_context)],
    state: Annotated[AppState, Depends(get_app_state)],
) -> dict:
    from ..models import Account
    from ..services.ledger import recompute_balance

    with state.db.session(ctx.tenant) as session:
        account = session.execute(
            select(Account).where(Account.customer_id == customer_id)
        ).scalar_one_or_none()
        if account is None:
            raise not_found("账户", customer_id)
        # 对账校验：存储余额必须等于分录重算值
        recomputed = recompute_balance(
            session,
            account_code="PREPAID",
            customer_id=customer_id,
            currency=account.currency,
        )
        data = account_out(account)
        data["recomputed_balance_minor"] = recomputed
        data["consistent"] = recomputed == account.balance_minor
        return data
