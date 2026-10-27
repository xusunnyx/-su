"""数据访问层：连接管理 + 租户隔离策略。

两种隔离策略共用同一套业务代码，切换只改 ``TENANT_ISOLATION``：

* ``row`` 共享库，所有表带 ``tenant_id``，查询时自动注入过滤条件。
* ``schema`` 每租户独立库（PostgreSQL 的 schema-per-tenant 等价物）。

``row`` 模式下额外做了三件事，构成纵深防御（面试高频追问点）：

1. ``do_orm_execute`` 事件：任何 SELECT 自动追加 ``tenant_id = ?``，
   业务代码写 ``select(Order)`` 就不可能“忘了加 where”。
2. ``before_flush`` 事件：新对象的 ``tenant_id`` 与上下文不一致时直接抛错，
   防止「查 A 租户、写 B 租户」这种越权写入。
3. 查询一律走 ``select()`` 而非 ``session.get()``（后者可能命中身份映射，
   绕过上面的过滤）。
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Iterator

from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker, with_loader_criteria

from .config import Settings
from .errors import TenantViolation
from .models import Base, TENANT_SCOPED_MODELS, Tenant

logger = logging.getLogger(__name__)


def _json_default(value: Any) -> Any: 
    """JSON 列里允许出现时间类型（幂等响应快照需要原样存下来）。"""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None: # 配置 SQLite 连接
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()


@event.listens_for(Session, "do_orm_execute") # 自动给所有 SELECT 加上租户条件
def _inject_tenant_filter(execute_state: Any) -> None: 
    """自动给所有 SELECT 加上租户条件——这是行级隔离的核心保障。"""
    if not execute_state.is_select:
        return
    tenant_id = execute_state.session.info.get("tenant_id")
    if tenant_id is None:
        return

    statement = execute_state.statement
    for model in TENANT_SCOPED_MODELS:
        statement = statement.options(
            with_loader_criteria(
                model,
                lambda cls: cls.tenant_id == tenant_id,
                include_aliases=True,
            )
        )
    execute_state.statement = statement


@event.listens_for(Session, "before_flush") # 检查写入的 tenant_id 是否与上下文一致
def _guard_cross_tenant_writes(session: Session, _flush_ctx: Any, _instances: Any) -> None:
    """纵深防御第二层：写入的 tenant_id 必须与当前上下文一致。"""
    tenant_id = session.info.get("tenant_id")
    if tenant_id is None:
        return
    for obj in session.new:
        if isinstance(obj, TENANT_SCOPED_MODELS):
            obj_tenant = getattr(obj, "tenant_id", None)
            if obj_tenant != tenant_id:
                raise TenantViolation(
                    f"拒绝跨租户写入: model={type(obj).__name__} "
                    f"obj.tenant_id={obj_tenant} context={tenant_id}"
                )


class Database:
    """按租户解析 Engine / Session 的小型连接管理器。"""

    def __init__(self, settings: Settings) -> None: # 初始化数据库连接
        self.settings = settings
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        (self.settings.data_dir / "tenants").mkdir(parents=True, exist_ok=True)

        self._engines: dict[str, Engine] = {}
        self._makers: dict[str, sessionmaker[Session]] = {}

        if settings.database_url: # 共享库模式
            control_url = settings.database_url
        else:
            control_path = (settings.data_dir / "control.db").resolve()
            control_url = f"sqlite:///{control_path.as_posix()}"

        self.control_url = control_url
        self.control: Engine = self._build_engine(control_url, "control")

    # ------------------------------------------------------------------ #
    # Engine 管理
    # ------------------------------------------------------------------ #
    def _build_engine(self, url: str, key: str) -> Engine: # 构建数据库连接
        if key in self._engines:
            return self._engines[key]

        kwargs: dict[str, Any] = { # 数据库连接参数
            "future": True,
            "pool_pre_ping": True,
            "json_serializer": lambda obj: json.dumps(obj, ensure_ascii=False, default=_json_default),
        }
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}

        engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):
            event.listen(engine, "connect", _sqlite_pragmas)
        self._engines[key] = engine
        return engine

    def engine_for(self, tenant: Tenant | None) -> Engine: # 获取数据库连接
        if tenant is None or self.settings.isolation == "row":
            return self.control

        key = f"tenant:{tenant.slug}"
        if key not in self._engines: # 构建数据库连接
            engine = self._build_engine(self.settings.tenant_db_url(tenant.slug), key)
            Base.metadata.create_all(engine)
            self._provision_tenant_metadata(engine, tenant)
        return self._engines[key]

    @staticmethod
    def _provision_tenant_metadata(engine: Engine, tenant: Tenant) -> None:
        """独立库模式：把租户行复制一份到租户库。
        租户库里的 ``tenant_id`` 外键指向本库的 ``tenants`` 表，少了这行会
        直接违反外键。好处是每个租户库自包含，备份/迁移/删库都只涉及一个文件。
        """
        with Session(engine) as session:
            if session.get(Tenant, tenant.id) is None:
                session.add(
                    Tenant(
                        id=tenant.id,
                        slug=tenant.slug,
                        name=tenant.name,
                        api_key_hash=tenant.api_key_hash,
                        status=tenant.status,
                    )
                )
                session.commit()

    def sessionmaker_for(self, tenant: Tenant | None) -> sessionmaker[Session]:
        engine = self.engine_for(tenant)
        key = str(engine.url)
        maker = self._makers.get(key)
        if maker is None:
            maker = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
            self._makers[key] = maker
        return maker

    # ------------------------------------------------------------------ #
    # 事务
    # ------------------------------------------------------------------ #
    @contextmanager
    def session(self, tenant: Tenant | None = None) -> Iterator[Session]:
        """一个上下文 = 一个事务；异常自动回滚。""" #业务代码不会出现“忘了提交/忘了回滚”。
        session = self.sessionmaker_for(tenant)()
        if tenant is not None:
            session.info["tenant_id"] = tenant.id
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # 启动 / 运维
    # ------------------------------------------------------------------ #
    def init(self) -> None:
        """建表。schema 模式下租户库在首次访问时懒创建。"""
        Base.metadata.create_all(self.control)
        logger.info(
            "database ready: isolation=%s control=%s",
            self.settings.isolation,
            self.control_url,
        )

    def list_tenants(self) -> list[Tenant]: # 获取所有租户
        with self.session(None) as s:
            return list(s.execute(select(Tenant).order_by(Tenant.id)).scalars().all())

    def dispose(self) -> None: # 关闭数据库连接
        for engine in self._engines.values():
            engine.dispose()
        self._engines.clear()
        self._makers.clear()
