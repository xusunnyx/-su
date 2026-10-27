"""认证与凭证：API Key 只存哈希，租户身份来自凭证而不是请求参数。

这一点是「数据隔离」的真正入口：如果租户 ID 由客户端传参决定，
那行级过滤写得再漂亮也能被越权打穿。
"""

from __future__ import annotations

import hashlib
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Tenant

API_KEY_PREFIX = "adf_"


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_api_key() -> str:
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


def find_tenant_by_api_key(session: Session, raw_key: str) -> Tenant | None:
    """控制面查询：不带租户过滤（此时还不知道是哪个租户）。"""
    key_hash = hash_api_key(raw_key)
    return session.execute(
        select(Tenant).where(Tenant.api_key_hash == key_hash, Tenant.status == "ACTIVE")
    ).scalar_one_or_none()
