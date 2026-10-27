"""幂等执行器。

为什么不能只写「先查后插」：
    两个请求同时到达，都查不到记录，于是都执行了业务 —— 重复扣款。
真正的保证来自 ``(tenant_id, key)`` 上的唯一索引：谁先 flush 谁赢，
输家拿到 IntegrityError，重试一次就能读到赢家已提交的响应并原样重放。

时序（全部在同一个事务里）::

    INSERT idempotency_record(key, hash, status=IN_PROGRESS)   <- 唯一索引挡并发
    ...业务写入（订单、分录、outbox）...
    UPDATE idempotency_record SET status=COMPLETED, response=...
    COMMIT                                                      <- 原子提交

因此不存在「订单已建、幂等记录没写」的中间态。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from .db import Database
from .errors import AppError, conflict
from .models import IdempotencyRecord, Tenant, utcnow

logger = logging.getLogger(__name__)

#: fn(session) -> (status_code, response_body, resource_id)
Handler = Callable[[Session], "tuple[int, dict, str | None]"]


@dataclass 
class IdempotentOutcome:
    status_code: int
    body: dict
    replayed: bool
    resource_id: str | None = None


def run_idempotent( # 幂等执行器
    db: Database,
    tenant: Tenant,
    key: str,
    request_hash: str,
    fn: Handler,
    *,
    ttl_seconds: int = 24 * 3600,
    max_attempts: int = 3,
) -> IdempotentOutcome:
    """在一个事务里完成「幂等表中占位 -> 业务写入 -> 记录响应」。
    冲突重试只针对唯一索引冲突和 SQLite 写锁，业务异常直接透传。
    """
    for attempt in range(1, max_attempts + 1):
        try:
            with db.session(tenant) as session:
                return _execute_once(session, tenant, key, request_hash, fn, ttl_seconds)
        except IntegrityError:
            # 并发同键：赢家已提交，重试一次即可读到 COMPLETED 记录
            if attempt < max_attempts:
                logger.info("idempotency race, retrying (attempt=%s, key=%s)", attempt, key)
                time.sleep(0.01 * attempt)
                continue
            raise conflict("IDEMPOTENCY_CONFLICT", "幂等键冲突，请重试")
        except OperationalError as exc:
            if "locked" in str(exc).lower() and attempt < max_attempts:
                time.sleep(0.05 * attempt)
                continue
            raise
    raise RuntimeError("unreachable")  # pragma: no cover


def _execute_once( # 幂等执行器内部实现
    session: Session,
    tenant: Tenant,
    key: str,
    request_hash: str,
    fn: Handler,
    ttl_seconds: int,
) -> IdempotentOutcome:
    now = utcnow()
    record = session.execute(
        select(IdempotencyRecord).where(IdempotencyRecord.key == key)
    ).scalar_one_or_none()

    # 过期记录直接作废，允许复用同一个 key（配合 TTL 清理策略）
    if record is not None and record.expires_at is not None and record.expires_at <= now:
        session.delete(record)
        session.flush()
        record = None

    if record is not None:
        if record.request_hash != request_hash:
            raise conflict(
                "IDEMPOTENCY_KEY_REUSED",
                "同一个 Idempotency-Key 被用于不同的请求体",
                key=key,
            )
        if record.status != "COMPLETED":
            raise AppError(
                409,
                "IDEMPOTENCY_IN_PROGRESS",
                "相同幂等键的请求正在处理中，请稍后重试",
                headers={"Retry-After": "1"},
            )
        return IdempotentOutcome(
            status_code=record.status_code,
            body=record.response_body or {},
            replayed=True,
            resource_id=record.resource_id,
        )

    record = IdempotencyRecord(
        tenant_id=tenant.id,
        key=key,
        request_hash=request_hash,
        status="IN_PROGRESS",
        expires_at=now + timedelta(seconds=ttl_seconds),
    )
    session.add(record)
    session.flush()  # <- 并发冲突在这里暴露，并触发整个事务回滚

    status_code, body, resource_id = fn(session)

    record.status = "COMPLETED"
    record.status_code = status_code
    record.response_body = body
    record.resource_id = resource_id
    session.flush()

    return IdempotentOutcome(
        status_code=status_code, body=body, replayed=False, resource_id=resource_id
    )
