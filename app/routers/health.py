"""健康检查与根路由。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import text

from ..deps import get_app_state
from ..state import AppState

router = APIRouter(tags=["meta"])


@router.get("/healthz", summary="健康检查：数据库 + 缓存")  # 健康检查路由
def healthz(state: Annotated[AppState, Depends(get_app_state)]) -> dict:
    db_ok = True
    try:
        with state.db.session(None) as session:
            session.execute(text("SELECT 1"))
    except Exception:  # pragma: no cover
        db_ok = False

    return {
        "status": "ok" if db_ok else "degraded",
        "database": db_ok,
        "cache": {"backend": state.cache.name, "alive": state.cache.ping()},
        "isolation": state.settings.isolation,
        "worker": state.worker is not None,
    }


@router.get("/", include_in_schema=False)  # 根路由
def index() -> dict:
    return {
        "name": "AdFlow - 多租户广告 CRM 与交易平台 Demo",
        "docs": "/docs",
        "health": "/healthz",
        "admin": "/api/v1/admin/tenants",
        "business": "/api/v1/orders",
    }
