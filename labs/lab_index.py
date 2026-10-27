"""实验 2：索引到底在干什么。

    python -m labs.lab_index

目的：用 SQLite 的 EXPLAIN QUERY PLAN 亲眼看到 SCAN（全表扫）变成 SEARCH（走索引），
并量出耗时差异。数据库里"慢"的绝大部分原因，都能在这一屏里找到答案。
"""

from __future__ import annotations

import sqlite3
import tempfile
import time
from pathlib import Path

ROWS = 100_000
TENANTS = 10
QUERY = "SELECT count(*) FROM orders WHERE tenant_id = 3 AND status = 'PAID'"
REPEAT = 100


def build_db() -> sqlite3.Connection:
    path = Path(tempfile.mkdtemp(prefix="lab-index-")) / "shop.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE orders (
            id          INTEGER PRIMARY KEY,
            tenant_id   INTEGER NOT NULL,
            status      TEXT    NOT NULL,
            amount_minor INTEGER NOT NULL,
            created_at  TEXT    NOT NULL
        )
        """
    )
    rows = (
        (
            i,
            i % TENANTS + 1,
            "PAID" if i % 3 else "PENDING_PAYMENT",
            (i % 500) * 100,
            "2026-10-01 00:00:00",
        )
        for i in range(1, ROWS + 1)
    )
    conn.executemany(
        "INSERT INTO orders (id, tenant_id, status, amount_minor, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return conn


def plan(conn: sqlite3.Connection) -> str:
    rows = conn.execute("EXPLAIN QUERY PLAN " + QUERY).fetchall()
    return " | ".join(row[3] for row in rows)


def timeit(conn: sqlite3.Connection, repeat: int = REPEAT) -> float:
    started = time.perf_counter()
    for _ in range(repeat):
        conn.execute(QUERY).fetchone()
    return (time.perf_counter() - started) / repeat * 1000


def main() -> int:
    print(f"建表并写入 {ROWS:,} 行订单数据（10 个租户）...\n")
    conn = build_db()

    before_plan = plan(conn)
    before_ms = timeit(conn)
    print("【没有索引】")
    print(f"  执行计划: {before_plan}")
    print(f"  平均耗时: {before_ms:.3f} ms/次\n")

    print("创建复合索引 (tenant_id, status)...")
    conn.execute("CREATE INDEX ix_orders_tenant_status ON orders (tenant_id, status)")
    conn.commit()

    after_plan = plan(conn)
    after_ms = timeit(conn)
    print("\n【有索引】")
    print(f"  执行计划: {after_plan}")
    print(f"  平均耗时: {after_ms:.3f} ms/次")
    print(f"  提速约 {before_ms / max(after_ms, 1e-9):.1f} 倍\n")

    print("三个必须记住的点：")
    print("1. SCAN = 从头翻到尾；SEARCH ... USING INDEX = 直接翻到那一页。")
    print("2. 索引有顺序：(tenant_id, status) 能用上「先按租户、再按状态」，")
    print("   反过来 (status, tenant_id) 对「按租户查全部状态」就不够好。")
    print("3. 索引不是越多越好：每次写入都要维护索引，占空间、拖慢插入。")
    print("\n对照真实代码：app/models.py 里每个租户表的 __table_args__。")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
