"""FastAPI 应用装配：中间件、异常处理、路由注册、生命周期。

启动：``uvicorn app.main:create_app --factory --reload``
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import __version__
from .config import Settings
from .errors import AppError, TenantViolation
from .responses import UTF8JSONResponse
from .routers import admin, customers, health, orders, stats
from .state import AppState
from .workers import OutboxWorker

logger = logging.getLogger("adflow")


def _configure_logging() -> None: #配置日志记录
    root = logging.getLogger()
    if not root.handlers: #如果没有日志处理器
        level = os.getenv("LOG_LEVEL", "INFO").upper()
        logging.basicConfig( 
            level=getattr(logging, level, logging.INFO),
            format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
        )


def create_app(settings: Settings | None = None) -> FastAPI: #创建 FastAPI 应用
    _configure_logging() #配置日志记录
    settings = settings or Settings.from_env()
    state = AppState.build(settings) #创建应用状态

    @asynccontextmanager #异步上下文管理器：用于异步资源的管理，如数据库连接、文件连接等
    async def lifespan(_app: FastAPI): #_app 是 FastAPI 实例，用于获取应用状态
        state.db.init() 
        worker: OutboxWorker | None = None  
        if settings.worker_enabled: #按配置决定是否启动 Outbox 后台线程
            worker = OutboxWorker(state, settings.worker_interval) #Outbox 通常是一个后台任务：不断扫描数据库里的“待发送消息”，然后发出去。
            worker.start()
        state.worker = worker
        state.started = True
        logger.info(
            "AdFlow 启动 | isolation=%s cache=%s worker=%s", 
            settings.isolation,
            state.cache.name,
            "on" if worker else "off",
        )
        try: #遇到 yield，暂停在这里，应用开始处理请求……
            yield # 启动时执行Yeild之前的：建表、启动后台线程，# 关闭时执行Yeild之后：停线程、释放数据库连接
        finally:
            if worker is not None:
                worker.stop()
            state.db.dispose()
            logger.info("AdFlow 已停止")

    app = FastAPI( #创建 FastAPI 实例
        title=settings.app_name,
        version=__version__,
        default_response_class=UTF8JSONResponse,
        description=(
            "多租户广告 CRM + 交易流水管理 Demo。\n\n"
            "- 租户隔离：行级自动过滤 / 独立库两套策略\n"
            "- 资金安全：复式记账 + 条件更新 + 复式分录对账\n"
            "- 交易可靠性：幂等键 + 唯一索引 + 事务性 Outbox\n"
            "- 高并发读：cache-aside 单飞缓存 / 写路径增量计数 / 滑动窗口限流"
        ),
        lifespan=lifespan,
    )
    app.state.ctx = state #把应用状态挂到 FastAPI 实例的 state 上，方便在路由处理函数中访问

    # ------------------------------------------------------------------ #
    # 中间件：Request-Id + 结构化访问日志（不打印请求体，避免泄露密钥）
    # ------------------------------------------------------------------ #
    @app.middleware("http")   #装饰器：把下面的这个函数注册为 HTTP 中间件，之后每一个 HTTP 请求都先交给它处理
    async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = request.headers.get("X-Request-Id") or f"req_{uuid.uuid4().hex[:12]}"  #带ID沿用/不带则生成
        request.state.request_id = request_id  #每次请求一个state变量
        started = time.perf_counter() #开始计时
        
        try:
            response = await call_next(request) #把控制权交给FastAPI内层，处理路由、 deps.py 的租户认证 、限流、业务函数
        except Exception:
            logger.exception("unhandled error | rid=%s path=%s", request_id, request.url.path)
            raise #如果内层抛出了 连异常处理器都没接住 的异常，这里补一条带请求 ID 的错误日志，然后`raise` 原样抛出。
        #注意它没有吞掉异常 ——吞掉会导致调用方拿到一个不完整的响应，正确做法是记录后继续抛，交给外层兜底。

        elapsed_ms = (time.perf_counter() - started) * 1000 #计时结束，算出毫秒耗时；

        response.headers["X-Request-Id"] = request_id #把请求ID添加到响应头
        logger.info(
            json.dumps(
                {
                    "rid": request_id,#记录请求ID
                    "method": request.method,#记录请求方法
                    "path": request.url.path,#记录请求路径
                    "status": response.status_code,#记录响应状态码
                    "ms": round(elapsed_ms, 2), #记录耗时
                    "tenant": getattr(request.state, "tenant_slug", None),#记录租户ID
                },
                ensure_ascii=False,
            )
        )
        return response
    #刻意不打印请求体 ，防止密钥、资金数据泄进日志文件。

    # ------------------------------------------------------------------ #
    # 异常处理：稳定错误码，前端/告警可直接分支
    # ------------------------------------------------------------------ #
    @app.exception_handler(AppError) #处理 AppError 异常 （业务异常）
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return UTF8JSONResponse(
            status_code=exc.status_code,
            content={
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
                "request_id": getattr(request.state, "request_id", None),
            },
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError) #处理 RequestValidationError 异常
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return UTF8JSONResponse( 
            status_code=422,
            content={
                "code": "VALIDATION_ERROR",
                "message": "请求参数校验失败",
                "details": {"errors": json.loads(json.dumps(exc.errors(), default=str))},
                "request_id": getattr(request.state, "request_id", None),
            },
        )

    @app.exception_handler(TenantViolation) #处理 TenantViolation 异常
    async def handle_tenant_violation(
        request: Request, exc: TenantViolation
    ) -> JSONResponse:
        logger.error("TENANT VIOLATION: %s", exc)
        return UTF8JSONResponse(
            status_code=500,
            content={
                "code": "TENANT_VIOLATION",
                "message": "检测到跨租户写入，请求已被拒绝",
                "details": {},
                "request_id": getattr(request.state, "request_id", None),
            },
        )

    @app.exception_handler(Exception) #处理 Exception 异常（未知异常）
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        return UTF8JSONResponse(
            status_code=500,
            content={
                "code": "INTERNAL_ERROR",
                "message": "服务内部错误",
                "details": {},
                "request_id": getattr(request.state, "request_id", None),
            },
        )

    app.include_router(health.router) #包含健康检查路由
    app.include_router(admin.router, prefix="/api/v1") #包含管理员路由
    app.include_router(customers.router, prefix="/api/v1") #包含客户路由
    app.include_router(orders.router, prefix="/api/v1") #包含订单路由
    app.include_router(stats.router, prefix="/api/v1") #包含统计路由

    return app #返回FastAPI应用


def __getattr__(name: str) -> Any:  # pragma: no cover - 兼容 uvicorn app.main:app
    if name == "app": #如果请求的是app变量，就返回创建的FastAPI应用
        return create_app()
    raise AttributeError(name) #如果请求的是其他变量，就抛出异常


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8000) #启动FastAPI应用
