"""配置：全部来自环境变量，默认值保证「零外部依赖」就能跑起来。

设计取舍
--------
真实生产环境是 MySQL/PostgreSQL + Redis 集群，但 demo 的第一诉求是
"clone 下来 10 秒能跑"。所以这里把基础设施做成可插拔：

* ``DATABASE_URL`` 不填 -> SQLite（文件型，零安装）
* ``REDIS_URL``   不填 -> 进程内内存缓存/限流器（接口语义与 Redis 版一致）
* ``TENANT_ISOLATION=row``    共享库 + tenant_id 行级隔离
* ``TENANT_ISOLATION=schema`` 每租户一个独立库（模拟 schema-per-tenant）
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

VALID_ISOLATIONS = ("row", "schema")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


@dataclass
class Settings:
    app_name: str = "AdFlow CRM & Trading Demo"
    env: str = "dev"

    #: row = 共享库行级隔离；schema = 每租户独立库
    isolation: str = "row"
    #: SQLite 等本地文件、日志的根目录
    data_dir: Path = field(default_factory=lambda: Path("data"))
    #: 真实部署时填 postgresql+psycopg://... 或 mysql+pymysql://...
    database_url: str | None = None
    #: 不填则自动降级到进程内实现
    redis_url: str | None = None

    #: 统计报表缓存 TTL（秒）
    cache_ttl: int = 15
    #: 单租户滑窗限流：window 秒内最多 limit 次
    rate_limit: int = 120
    rate_window: int = 60
    #: 幂等键保留时长（秒）
    idempotency_ttl: int = 24 * 3600

    #: 后台 outbox 派发线程
    worker_enabled: bool = True
    worker_interval: float = 0.5

    #: 创建租户的管理员令牌（demo 用，生产应走 SSO/网关）
    admin_token: str = "dev-admin-token"
    #: 默认币种，金额一律用「最小货币单位」的整数存储（分）
    currency: str = "USD"

    @classmethod
    def from_env(cls) -> "Settings":
        isolation = os.getenv("TENANT_ISOLATION", "row").strip().lower()
        if isolation not in VALID_ISOLATIONS:
            raise ValueError(f"TENANT_ISOLATION 只能是 {VALID_ISOLATIONS}，收到 {isolation!r}")
        return cls(
            env=os.getenv("APP_ENV", "dev"),
            isolation=isolation,
            data_dir=Path(os.getenv("DATA_DIR", "data")),
            database_url=os.getenv("DATABASE_URL") or None,
            redis_url=os.getenv("REDIS_URL") or None,
            cache_ttl=_env_int("CACHE_TTL", 15),
            rate_limit=_env_int("RATE_LIMIT", 120),
            rate_window=_env_int("RATE_WINDOW", 60),
            idempotency_ttl=_env_int("IDEMPOTENCY_TTL", 24 * 3600),
            worker_enabled=_env_bool("WORKER_ENABLED", True),
            worker_interval=float(os.getenv("WORKER_INTERVAL", "0.5")),
            admin_token=os.getenv("ADMIN_TOKEN", "dev-admin-token"),
            currency=os.getenv("CURRENCY", "USD"),
        )

    @property
    def sqlite(self) -> bool:
        return (self.database_url or "").startswith("sqlite") or not self.database_url

    def tenant_db_url(self, slug: str) -> str:
        """schema-per-tenant 模式下每个租户的数据库地址。"""
        if not self.sqlite:
            base = self.database_url or ""
            # postgresql://.../adflow  ->  postgresql://.../adflow_tenant_<slug>
            return base.rsplit("/", 1)[0] + f"/adflow_{slug}"
        path = (self.data_dir / "tenants" / f"{slug}.db").resolve()
        return f"sqlite:///{path.as_posix()}"
