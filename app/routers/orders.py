"""订单与交易接口。

所有写接口都做三件事：租户鉴权 -> 滑窗限流 -> 幂等。顺序不是随便排的：
先鉴权才知道给谁限流，先限流才轮得到幂等表。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Response

from ..deps import RequestContext, enforce_rate_limit, get_app_state, get_context
from ..schemas import LedgerEntryOut, OrderCreate, OrderOut
from ..services.orders import OrderService
from ..state import AppState

router = APIRouter( # 订单路由
    prefix="/orders",
    tags=["orders"],
    dependencies=[Depends(get_context), Depends(enforce_rate_limit)],
)

IdempotencyKey = Annotated[ # 幂等键
    str,
    Header(
        alias="Idempotency-Key",
        min_length=8,
        max_length=128,
        description="客户端生成的唯一键（建议 UUID）。相同键 + 相同请求体重放结果。",
    ),
]
OptionalIdempotencyKey = Annotated[ # 可选幂等键
    str | None,
    Header(alias="Idempotency-Key", max_length=128),
]


@router.post("", response_model=OrderOut, status_code=201, summary="创建订单（必须带幂等键）")
def create_order( # 创建订单路由
    payload: OrderCreate,
    idempotency_key: IdempotencyKey,
    response: Response,
    ctx: Annotated[RequestContext, Depends(get_context)],
    state: Annotated[AppState, Depends(get_app_state)],
) -> dict:
    outcome = OrderService(state).create_order(ctx.tenant, payload, idempotency_key)
    response.status_code = outcome.status_code
    if outcome.replayed:
        response.headers["X-Idempotent-Replay"] = "true"
    return outcome.body


@router.get("", summary="订单列表（游标分页，杜绝深分页）")
def list_orders( # 查看订单列表路由
    ctx: Annotated[RequestContext, Depends(get_context)],
    state: Annotated[AppState, Depends(get_app_state)],
    status: Annotated[str | None, Query(max_length=24)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[int | None, Query(ge=1)] = None,
) -> dict:
    return OrderService(state).list_orders(
        ctx.tenant, status=status, limit=limit, cursor=cursor
    )


@router.get("/{order_no}", response_model=OrderOut, summary="订单详情")
def get_order( # 查看订单详情路由
    order_no: str,
    ctx: Annotated[RequestContext, Depends(get_context)],
    state: Annotated[AppState, Depends(get_app_state)],
) -> dict:
    return OrderService(state).get_order(ctx.tenant, order_no)


@router.post("/{order_no}/pay", response_model=OrderOut, summary="支付订单（状态机 + 余额条件更新）")
def pay_order( # 支付订单路由
    order_no: str,
    response: Response,
    ctx: Annotated[RequestContext, Depends(get_context)],
    state: Annotated[AppState, Depends(get_app_state)],
    idempotency_key: OptionalIdempotencyKey = None,
) -> dict:
    outcome = OrderService(state).pay_order(ctx.tenant, order_no, idempotency_key)
    response.status_code = outcome.status_code
    if outcome.replayed:
        response.headers["X-Idempotent-Replay"] = "true"
    return outcome.body


@router.post("/{order_no}/refund", response_model=OrderOut, summary="退款（反向分录）")
def refund_order( # 退款订单路由
    order_no: str,
    response: Response,
    ctx: Annotated[RequestContext, Depends(get_context)],
    state: Annotated[AppState, Depends(get_app_state)],
    idempotency_key: OptionalIdempotencyKey = None,
) -> dict:
    outcome = OrderService(state).refund_order(ctx.tenant, order_no, idempotency_key)
    response.status_code = outcome.status_code
    if outcome.replayed:
        response.headers["X-Idempotent-Replay"] = "true"
    return outcome.body


@router.get( # 查看订单的资金分录路由
    "/{order_no}/ledger",
    response_model=list[LedgerEntryOut],
    summary="查看订单的资金分录（借贷必须相等）",
)
def ledger( # 查看订单的资金分录路由
    order_no: str,
    ctx: Annotated[RequestContext, Depends(get_context)],
    state: Annotated[AppState, Depends(get_app_state)],
) -> list[dict]:
    return OrderService(state).list_ledger(ctx.tenant, order_no)
