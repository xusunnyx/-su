"""缓存与限流的统一抽象。

同一个接口，两套实现：

* :class:`RedisCache` —— 生产形态（Redis 原子操作、滑动窗口 ZSET、SETNX 锁）
* :class:`MemoryCache` —— 零依赖降级（进程内字典 + 线程锁）

业务代码只依赖 :class:`Cache` 抽象，所以「有没有 Redis」不影响功能正确性，
只影响多实例部署时的一致性——这正是面试时该讲清楚的边界。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any

try:  # redis 是可选依赖
    import redis as redis_lib
except Exception:  # pragma: no cover - 未安装 redis 包
    redis_lib = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


class Cache(ABC): 
    name: str = "abstract"

    @abstractmethod
    def get(self, key: str) -> Any | None: ...

    @abstractmethod
    def set(self, key: str, value: Any, ttl: int | None = None) -> None: ...

    @abstractmethod
    def delete(self, *keys: str) -> None: ...

    @abstractmethod
    def incr(self, key: str, amount: int = 1, ttl: int | None = None) -> int: ...

    @abstractmethod
    def sliding_window_allow(self, key: str, limit: int, window: int) -> bool:
        """滑动窗口限流：window 秒内允许 limit 次，原子实现。"""

    @abstractmethod
    def acquire_lock(self, key: str, ttl: int = 10) -> bool: ...

    @abstractmethod
    def release_lock(self, key: str) -> None: ...

    def ping(self) -> bool:
        return True

    def stats(self) -> dict[str, Any]:
        return {"backend": self.name}


class MemoryCache(Cache): # 内存缓存实现
    """单进程实现。仅用于本地开发/测试，多副本部署会各算各的。"""

    name = "memory"

    def __init__(self) -> None:
        self._data: dict[str, tuple[float | None, Any]] = {}
        self._windows: dict[str, list[float]] = {}
        self._locks: dict[str, float] = {}
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0

    def _expired(self, key: str) -> bool: # 检查缓存项是否过期
        item = self._data.get(key)
        if item is None:
            return True
        expires_at, _ = item
        if expires_at is not None and expires_at <= time.time():
            self._data.pop(key, None)
            return True
        return False

    def get(self, key: str) -> Any | None: 
        with self._lock:
            if self._expired(key):
                self._misses += 1
                return None
            self._hits += 1
            return self._data[key][1]

    def set(self, key: str, value: Any, ttl: int | None = None) -> None: # 设置缓存项
        with self._lock:
            expires_at = time.time() + ttl if ttl else None
            self._data[key] = (expires_at, value)

    def delete(self, *keys: str) -> None: # 删除缓存项
        with self._lock:
            for key in keys:
                self._data.pop(key, None)

    def incr(self, key: str, amount: int = 1, ttl: int | None = None) -> int: # 厞加缓存项值
        with self._lock:
            current = 0 if self._expired(key) else int(self._data[key][1])
            value = current + amount
            expires_at = self._data[key][0] if key in self._data else None
            if expires_at is None and ttl:
                expires_at = time.time() + ttl
            self._data[key] = (expires_at, value)
            return value

    def sliding_window_allow(self, key: str, limit: int, window: int) -> bool: # 滑动窗口限流
        now = time.time()
        with self._lock:
            hits = [ts for ts in self._windows.get(key, []) if ts > now - window]
            if len(hits) >= limit:
                self._windows[key] = hits
                return False
            hits.append(now)
            self._windows[key] = hits
            return True

    def acquire_lock(self, key: str, ttl: int = 10) -> bool: # 获取锁
        now = time.time()
        with self._lock:
            expires_at = self._locks.get(key)
            if expires_at is not None and expires_at > now:
                return False
            self._locks[key] = now + ttl
            return True

    def release_lock(self, key: str) -> None: # 释放锁
        with self._lock:
            self._locks.pop(key, None)

    def stats(self) -> dict[str, Any]: # 获取缓存统计信息
        with self._lock:
            total = self._hits + self._misses
            return {
                "backend": self.name,
                "keys": len(self._data),
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / total, 3) if total else 0.0,
            }


class RedisCache(Cache): # Redis 缓存实现
    name = "redis"

    def __init__(self, url: str) -> None:
        if redis_lib is None:  # pragma: no cover
            raise RuntimeError("redis 包未安装")
        self._client = redis_lib.Redis.from_url(url, decode_responses=True)

    # ---- 基础 KV（JSON 由调用方负责，这里只存字符串/整数） ---- #
    def get(self, key: str) -> Any | None:
        raw = self._client.get(key)
        if raw is None:
            return None
        try:
            import json

            return json.loads(raw)
        except Exception:
            return raw

    def set(self, key: str, value: Any, ttl: int | None = None) -> None: # 设置缓存项
        import json

        payload = json.dumps(value, ensure_ascii=False)
        if ttl:
            self._client.setex(key, ttl, payload)
        else:
            self._client.set(key, payload)

    def delete(self, *keys: str) -> None: # 删除缓存项
        if keys:
            self._client.delete(*keys)

    def incr(self, key: str, amount: int = 1, ttl: int | None = None) -> int: # 厞加缓存项值
        pipe = self._client.pipeline()
        pipe.incrby(key, amount)
        if ttl:
            pipe.expire(key, ttl)
        value = pipe.execute()[0]
        return int(value)

    def sliding_window_allow(self, key: str, limit: int, window: int) -> bool: # 滑动窗口限流
        now = time.time()
        member = f"{now:.6f}:{uuid.uuid4().hex[:8]}"
        pipe = self._client.pipeline()
        pipe.zremrangebyscore(key, 0, now - window)
        pipe.zadd(key, {member: now})
        pipe.zcard(key)
        pipe.expire(key, window + 1)
        count = int(pipe.execute()[2])
        if count > limit:
            self._client.zrem(key, member)
            return False
        return True

    def acquire_lock(self, key: str, ttl: int = 10) -> bool: # 获取锁
        return bool(self._client.set(key, "1", nx=True, ex=ttl))

    def release_lock(self, key: str) -> None:
        self._client.delete(key)

    def ping(self) -> bool: # 测试 Redis 连接
        try:
            return bool(self._client.ping())
        except Exception:
            return False


def build_cache(redis_url: str | None) -> Cache: 
    """有 Redis 就用 Redis，连不上则降级——保证 demo 永不因为中间件启动失败。"""
    if redis_url:
        try:
            cache = RedisCache(redis_url)
            if cache.ping():
                logger.info("cache backend = redis (%s)", redis_url)
                return cache
            logger.warning("Redis 无法连接，降级为进程内缓存: %s", redis_url)
        except Exception as exc:  # pragma: no cover - 依赖环境
            logger.warning("Redis 初始化失败(%s)，降级为进程内缓存", exc)
    return MemoryCache()
