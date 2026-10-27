"""统一错误模型：业务异常 -> 稳定的 JSON 错误码，便于客户端与告警系统消费。"""

from __future__ import annotations

from typing import Any


class AppError(Exception): 
    """可预期的业务错误（4xx）或受控的依赖错误（5xx）。"""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}
        self.headers = headers or {}


class TenantViolation(Exception):
    """写入了不属于当前租户的数据——属于代码缺陷，直接 500 并告警。"""


# ---- 常用错误构造器：让调用处只表达意图，不硬编码状态码 ---- #
def unauthorized(message: str = "缺少或无效的 API Key") -> AppError:
    return AppError(401, "UNAUTHORIZED", message)


def forbidden(message: str = "无权访问该资源") -> AppError:
    return AppError(403, "FORBIDDEN", message)


def not_found(resource: str, ident: Any) -> AppError:
    return AppError(404, "NOT_FOUND", f"{resource} 不存在", details={"id": str(ident)})


def conflict(code: str, message: str, **details: Any) -> AppError: # 冲突错误
    return AppError(409, code, message, details=details)


def unprocessable(code: str, message: str, **details: Any) -> AppError: # 无法处理错误
    return AppError(422, code, message, details=details)


def rate_limited(retry_after: int) -> AppError:
    return AppError(
        429,
        "RATE_LIMITED",
        "请求过于频繁，请稍后重试",
        details={"retry_after": retry_after},
        headers={"Retry-After": str(retry_after)},
    )
