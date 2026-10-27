"""统计与调试接口。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from ..deps import RequestContext, enforce_rate_limit, get_app_state, get_context
from ..services.stats import StatsService
from ..state import AppState
from ..workers import pending_count

router = APIRouter(
    prefix="/stats",
    tags=["stats"],
    dependencies=[Depends(get_context), Depends(enforce_rate_limit)],
)


@router.get("/summary", summary="近 N 天汇总（cache-aside + 单飞 + 版本号失效）")
def summary( # 查看近 N 天汇总路由
    ctx: Annotated[RequestContext, Depends(get_context)],
    state: Annotated[AppState, Depends(get_app_state)],
    days: Annotated[int, Query(ge=1, le=90)] = 7,
) -> dict:
    return StatsService(state).summary(ctx.tenant, days)


@router.get("/daily", summary="按天物化报表（outbox 异步生成，可重算）")
def daily( # 查看按天物化报表路由
    ctx: Annotated[RequestContext, Depends(get_context)],
    state: Annotated[AppState, Depends(get_app_state)],
    days: Annotated[int, Query(ge=1, le=90)] = 7,
) -> list[dict]:
    return StatsService(state).daily_reports(ctx.tenant, days)


@router.get("/realtime", summary="今日实时看板（Redis 写路径计数，O(1) 读）")
def realtime( # 查看今日实时看板路由
    ctx: Annotated[RequestContext, Depends(get_context)],
    state: Annotated[AppState, Depends(get_app_state)],
) -> dict:
    return StatsService(state).realtime(ctx.tenant)


@router.get("/debug/cache", summary="缓存命中率（演示缓存效果用）")
def cache_stats( # 查看缓存命中率路由
    ctx: Annotated[RequestContext, Depends(get_context)],
    state: Annotated[AppState, Depends(get_app_state)],
) -> dict:
    return StatsService(state).cache_stats()


@router.get("/debug/outbox", summary="待投递事件数量")
def outbox( # 查看待投递事件数量路由
    ctx: Annotated[RequestContext, Depends(get_context)],
    state: Annotated[AppState, Depends(get_app_state)],
) -> dict:
    return {"pending": pending_count(state)}
