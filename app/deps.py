"""HTTP 层公共依赖：请求上下文、限流。（执行顺序有讲究）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request

from .errors import rate_limited, unauthorized
from .models import Tenant
from .security import find_tenant_by_api_key
from .state import AppState


@dataclass(frozen=True) # 冻结数据类，防止修改
class RequestContext:
    tenant: Tenant
    request_id: str


def get_app_state(request: Request) -> AppState: # 获取应用状态
    return request.app.state.ctx  # type: ignore[attr-defined]


def get_context( # 从请求头`X-API-Key` 反查出租户身份（查不到就 401）
    request: Request,
    state: Annotated[AppState, Depends(get_app_state)],
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> RequestContext: # 获取请求上下文
    if not x_api_key:
        raise unauthorized("请在 X-API-Key 头中携带租户密钥")

    with state.db.session(None) as session: # 从数据库会话获取租户
        tenant = find_tenant_by_api_key(session, x_api_key)
    if tenant is None:
        raise unauthorized("租户不存在")

    # 供访问日志使用（中间件里能拿到，但依赖执行时已经解析过了）
    request.state.tenant_id = tenant.id  # 租户 ID
    request.state.tenant_slug = tenant.slug  # 租户 SLUG
    return RequestContext(tenant=tenant, request_id=getattr(request.state, "request_id", "-"))  # 返回请求上下文（把租户信息写进 RequestContext)再返回去


def enforce_rate_limit( # 限流
    ctx: Annotated[RequestContext, Depends(get_context)],     # 从依赖注入中获取请求上下文
    state: Annotated[AppState, Depends(get_app_state)],        # 从依赖注入中获取应用状态
    scope: str = "global",        # 限流范围，默认全局
) -> None:
    """滑动窗口限流：Redis 用 ZSET 原子实现，内存版用时间戳列表。"""
    settings = state.settings
    allowed = state.cache.sliding_window_allow( # 从流中获取是否允许请求，false时抛出rate_limit,
        f"rl:{ctx.tenant.slug}:{scope}",  
        limit=settings.rate_limit,  # 限流阈值
        window=settings.rate_window,  # 限流窗口
        scope=scope,  # 限流范围
    )
    if not allowed:
        raise rate_limited(retry_after=settings.rate_window)  # 限流异常，重试时间=窗口大小
        #客户端收到 HTTP 429 + 一个`Retry-After: 60` 响应头（告诉它“60 秒后再来”）
