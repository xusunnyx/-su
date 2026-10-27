"""应用级依赖容器：把配置、数据库、缓存、后台线程装在一起。

不用全局单例，是为了让测试可以针对每个用例构造独立的 ``AppState``
（临时目录 + 内存缓存 + 关闭后台线程）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .cache import Cache
from .config import Settings
from .db import Database


@dataclass
class AppState:
    settings: Settings
    db: Database
    cache: Cache
    worker: object | None = field(default=None)
    started: bool = False

    @classmethod
    def build(cls, settings: Settings, cache: Cache | None = None) -> "AppState":
        from .cache import build_cache

        return cls(
            settings=settings,
            db=Database(settings),
            cache=cache or build_cache(settings.redis_url),
        )
