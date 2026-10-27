"""租户生命周期：创建租户 + 签发 API Key（明文只返回一次）。"""

from __future__ import annotations

from sqlalchemy import select

from ..errors import conflict
from ..models import Tenant, utcnow
from ..security import generate_api_key, hash_api_key
from ..state import AppState


def create_tenant(state: AppState, *, slug: str, name: str) -> dict: # 创建租户
    api_key = generate_api_key()

    with state.db.session(None) as session: # 从数据库查询租户是否存在
        existing = session.execute(
            select(Tenant).where(Tenant.slug == slug)
        ).scalar_one_or_none()
        if existing is not None:
            raise conflict("TENANT_EXISTS", f"租户 {slug} 已存在")

        tenant = Tenant(
            slug=slug,
            name=name,
            api_key_hash=hash_api_key(api_key),
            created_at=utcnow(),
        )
        session.add(tenant)
        session.flush()

        # schema 模式下立刻建库建表，避免首个请求承担建表开销
        if state.settings.isolation == "schema":
            state.db.engine_for(tenant)

        return {
            "id": tenant.id,
            "slug": tenant.slug,
            "name": tenant.name,
            "status": tenant.status,
            "created_at": tenant.created_at,
            "api_key": api_key,
        }


def list_tenants(state: AppState) -> list[dict]: # 查询所有租户
    return [
        {
            "id": t.id,
            "slug": t.slug,
            "name": t.name,
            "status": t.status,
            "created_at": t.created_at,
        }
        for t in state.db.list_tenants()
    ]
