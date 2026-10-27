"""管理面：创建租户、查看租户。用 X-Admin-Token 保护。"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException

from ..deps import get_app_state
from ..schemas import TenantCreate, TenantCreated, TenantOut
from ..services.tenants import create_tenant, list_tenants
from ..state import AppState

router = APIRouter(prefix="/admin", tags=["admin"])


def require_admin( # 管理员令牌校验依赖项
    state: Annotated[AppState, Depends(get_app_state)],
    x_admin_token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
) -> None: # 校验管理员令牌
    if not x_admin_token or not secrets.compare_digest( 
        x_admin_token, state.settings.admin_token
    ):
        raise HTTPException(status_code=403, detail="管理员令牌无效")


@router.post( # 创建租户路由
    "/tenants",
    response_model=TenantCreated,
    status_code=201,
    dependencies=[Depends(require_admin)],
    summary="创建租户并签发 API Key（明文仅此一次）",
)
def create(payload: TenantCreate, state: Annotated[AppState, Depends(get_app_state)]) -> dict:
    return create_tenant(state, slug=payload.slug, name=payload.name)


@router.get( # 查看租户路由
    "/tenants",
    response_model=list[TenantOut],
    dependencies=[Depends(require_admin)],
)
def index(state: Annotated[AppState, Depends(get_app_state)]) -> list[dict]: # 查看租户列表
    return list_tenants(state)
