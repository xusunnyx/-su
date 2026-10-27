"""实验 3：打开数据库看看里面到底存了什么。

    python -m labs.inspect_db          # 有哪些表、字段、各多少行
    python -m labs.inspect_db --orders # 看最近 5 张订单
    python -m labs.inspect_db --break  # 故意插一条「只借不贷」的分录，然后去查对账接口

--break 之后调用 GET /customers/{id}/account，你会看到
`consistent: false` —— 这就是"派生值（余额）和真值（分录）对不上"的样子。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Settings  # noqa: E402

TABLE_ORDER = [
    "tenants",
    "customers",
    "accounts",
    "orders",
    "ledger_entries",
    "idempotency_records",
    "outbox_events",
    "daily_reports",
]


def db_path() -> Path:
    settings = Settings.from_env()
    return (settings.data_dir / "control.db").resolve()


def show_schema(conn: sqlite3.Connection) -> None:
    print(f"数据库文件: {conn.execute('PRAGMA database_list').fetchone()[2]}\n")
    for table in TABLE_ORDER:
        try:
            columns = conn.execute(f"PRAGMA table_info({table})").fetchall()
        except sqlite3.Error:
            continue
        rows = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        column_names = ", ".join(f"{c[1]}:{c[2]}" for c in columns)
        print(f"[{table}] {rows} 行")
        print(f"    {column_names}")


def show_orders(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT order_no, status, amount_minor, customer_id, created_at "
        "FROM orders ORDER BY id DESC LIMIT 5"
    ).fetchall()
    if not rows:
        print("还没有订单，先跑 python -m scripts.seed")
        return
    print("最近 5 张订单：")
    for order_no, status, amount, customer_id, created_at in rows:
        print(
            f"  {order_no}  {status:<17}{amount / 100:>9.2f}  客户 {customer_id:<3} "
            f"{str(created_at)[:19]}"
        )


def break_invariant(conn: sqlite3.Connection) -> None:
    tenant = conn.execute("SELECT id FROM tenants ORDER BY id LIMIT 1").fetchone()
    account = conn.execute(
        "SELECT customer_id, currency FROM accounts ORDER BY id LIMIT 1"
    ).fetchone()
    if tenant is None or account is None:
        print("数据还没准备好，先跑 python -m scripts.seed")
        return

    tenant_id = tenant[0]
    customer_id, currency = account
    conn.execute(
        "INSERT INTO ledger_entries "
        "(tenant_id, order_id, customer_id, account_code, direction, amount_minor, "
        " currency, biz_type, created_at) "
        "VALUES (?, NULL, ?, 'PREPAID', 'DEBIT', 5000, ?, 'PAYMENT', datetime('now'))",
        (tenant_id, customer_id, currency),
    )
    conn.commit()
    print(
        f"已插入一条「只借不贷」的分录：客户 {customer_id} 借方 50.00，没有对应的贷方。\n"
        f"现在去查：GET /api/v1/customers/{customer_id}/account\n"
        "你会看到 consistent=false，且 recomputed_balance_minor 比 balance_minor 少 5000。\n"
        "这就是对账的意义：钱看起来没变，但账已经不平了。"
    )


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    path = db_path()
    if not path.exists():
        print(f"找不到数据库 {path}，先运行：python -m scripts.seed")
        return 1

    conn = sqlite3.connect(path)
    if "--orders" in argv:
        show_orders(conn)
    elif "--break" in argv:
        break_invariant(conn)
    else:
        show_schema(conn)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
