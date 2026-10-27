"""统一的 JSON 响应类型。

显式带上 ``charset=utf-8``：Starlette 默认只发 ``application/json``，
而 Windows PowerShell 5.1 的 ``Invoke-RestMethod`` 在没有 charset 时会按
ISO-8859-1 解码，中文全变乱码。加上 charset 后 curl / PowerShell / 浏览器
看到的中文都一致。
"""

from __future__ import annotations

from fastapi.responses import JSONResponse


class UTF8JSONResponse(JSONResponse):
    media_type = "application/json; charset=utf-8"  # 显式指定 charset=utf-8
